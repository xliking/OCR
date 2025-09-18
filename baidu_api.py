from __future__ import annotations
import base64
import json
import time
import calendar
import datetime
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Tuple
import httpx
from fastapi import FastAPI, HTTPException, File, UploadFile, Form, Depends, Header
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings
from redis.asyncio import Redis
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv

# 加载.env文件
load_dotenv()


# https://ai.baidu.com/ai-doc/OCR/7ktb8md0j
class KeyItem(BaseModel):
    client_id: str
    client_secret: str


class Settings(BaseSettings):
    # 可通过环境变量 BAIDU_KEYS 覆盖
    baidu_keys: List[KeyItem] = Field(
        default_factory=list,
        env="BAIDU_KEYS",
        description="JSON 数组 [{client_id, client_secret}, ...]"
    )
    redis_url: str = Field("redis://localhost:6379/8", env="REDIS_URL")
    redis_password: Optional[str] = Field(None, env="REDIS_PASSWORD")
    token_max_uses: int = Field(900, env="TOKEN_MAX_USES")
    # 新增：月配额监控
    monthly_quota_limit: int = Field(1000, env="MONTHLY_QUOTA_LIMIT")
    qps_limit: int = Field(2, env="QPS_LIMIT")
    # 健康检查配置
    max_consecutive_errors: int = Field(3, env="MAX_CONSECUTIVE_ERRORS", description="连续错误次数阈值")
    health_check_interval: int = Field(3600, env="HEALTH_CHECK_INTERVAL", description="健康检查间隔（秒）")
    # API密钥校验 - 防止盗刷
    api_key: Optional[str] = Field(None, env="API_KEY", description="API访问密钥")

    @field_validator("api_key", mode="before")
    @classmethod
    def _convert_api_key_to_string(cls, v):
        if v is not None:
            return str(v)
        return v

    baidu_token_url: str = Field(
        "https://aip.baidubce.com/oauth/2.0/token",
        env="BAIDU_TOKEN_URL",
    )
    baidu_ocr_url: str = Field(
        "https://aip.baidubce.com/rest/2.0/ocr/v1/multiple_invoice",
        env="BAIDU_OCR_URL",
    )

    @field_validator("baidu_keys", mode="before")
    @classmethod
    def _parse_keys(cls, v):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if not isinstance(parsed, list):
                    raise ValueError("BAIDU_KEYS必须是JSON数组格式")
                result = [KeyItem(**item) for item in parsed]
                return result
            except json.JSONDecodeError as e:
                print(f"BAIDU_KEYS JSON格式错误: {e}")
                print(f"   原始值: {v}")
                print("   正确格式示例: [{'client_id':'xxx','client_secret':'yyy'}]")
                raise ValueError(f"BAIDU_KEYS JSON格式错误: {e}")
            except Exception as e:
                print(f"BAIDU_KEYS配置错误: {e}")
                print(f"   原始值: {v}")
                raise ValueError(f"BAIDU_KEYS配置错误: {e}")
        return v


settings = Settings()

# BAIDU_KEYS必须通过环境变量提供
if not settings.baidu_keys:
    raise ValueError("BAIDU_KEYS环境变量未配置或为空，请在.env文件中配置API密钥")


# ---------------------------
# Redis 辅助
# ---------------------------

class RedisStore:
    def __init__(self, url: str, password: Optional[str] = None):
        # 当 URL 未包含密码且提供了 REDIS_PASSWORD 时，拼入密码
        if password:
            parsed = urlparse(url)
            if not parsed.password:
                netloc = parsed.netloc
                # 插入 :password@
                if "@" in netloc:
                    # 已有用户名的复杂情况，此处简单处理为覆盖
                    userinfo, host = netloc.split("@", 1)
                    netloc = f":{password}@{host}"
                else:
                    netloc = f":{password}@{netloc}"
                url = urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))
        self._client = Redis.from_url(url)

    @property
    def client(self) -> Redis:
        return self._client

    async def close(self):
        await self._client.aclose()


# Redis 键：
#   rr:index                     -> 轮询计数器
#   token:{client_id}            -> Hash { token, remaining, expire_ts }
#   metrics:requests_total       -> 请求计数
#   metrics:upstream_errors_total-> 上游错误计数
#   monthly:{client_id}:{YYYY-MM} -> 月度使用计数
#   qps:{client_id}:{timestamp}  -> QPS 限制计数

# ---------------------------
# Token 管理器
# ---------------------------

class TokenManager:
    def __init__(self, store: RedisStore, keys: List[KeyItem], token_max_uses: int, monthly_quota_limit: int = 1000,
                 qps_limit: int = 2, max_consecutive_errors: int = 3, health_check_interval: int = 3600):
        self.store = store
        self.keys = keys
        self.token_max_uses = token_max_uses
        self.monthly_quota_limit = monthly_quota_limit
        self.qps_limit = qps_limit
        # 健康检查配置
        self.max_consecutive_errors = max_consecutive_errors
        self.health_check_interval = health_check_interval

    async def _get_healthy_keys(self) -> List[KeyItem]:
        """获取健康的API密钥列表"""
        healthy_keys = []
        current_time = int(time.time())

        for key in self.keys:
            # 检查密钥是否被标记为不健康
            health_key = f"health:{key.client_id}"
            health_data = await self.store.client.hgetall(health_key)

            if not health_data:
                # 没有健康数据，认为是健康的
                healthy_keys.append(key)
                continue

            decoded = {k.decode(): v.decode() for k, v in health_data.items()}
            is_unhealthy = decoded.get("unhealthy", "false") == "true"
            last_check = int(decoded.get("last_check", 0))

            if is_unhealthy:
                # 检查是否到了重新尝试的时间
                if current_time - last_check >= self.health_check_interval:
                    # 重置健康状态，给它一次机会
                    await self.store.client.hset(health_key, mapping={
                        "unhealthy": "false",
                        "consecutive_errors": "0",
                        "last_check": str(current_time)
                    })
                    healthy_keys.append(key)
            else:
                healthy_keys.append(key)

        return healthy_keys

    async def _rr_pick_key(self) -> KeyItem:
        # 优先从健康的密钥中选择
        healthy_keys = await self._get_healthy_keys()

        if not healthy_keys:
            # 如果没有健康的密钥，从所有密钥中选择（紧急情况）
            healthy_keys = self.keys

        if not healthy_keys:
            raise HTTPException(status_code=500, detail="未配置任何 BAIDU_KEYS")

        # 在健康密钥中轮询
        idx = await self.store.client.incr("rr:healthy_index")
        return healthy_keys[(idx - 1) % len(healthy_keys)]

    async def _record_key_error(self, key: KeyItem, error_msg: str):
        """记录API密钥错误，并检查是否需要标记为不健康"""
        health_key = f"health:{key.client_id}"
        current_time = int(time.time())

        # 获取当前错误计数
        health_data = await self.store.client.hgetall(health_key)
        if health_data:
            decoded = {k.decode(): v.decode() for k, v in health_data.items()}
            consecutive_errors = int(decoded.get("consecutive_errors", 0)) + 1
        else:
            consecutive_errors = 1

        # 记录错误但不打印详细信息

        # 更新错误计数
        mapping = {
            "consecutive_errors": str(consecutive_errors),
            "last_error": error_msg,
            "last_error_time": str(current_time)
        }

        # 检查是否需要标记为不健康
        # 1. 连续错误达到阈值
        # 2. 或者遇到致命错误（如invalid_client, invalid_secret等）
        is_critical_error = any(critical in error_msg.lower() for critical in [
            "invalid_client", "invalid_secret", "unknown client id", "client_id not found"
        ])
        
        if consecutive_errors >= self.max_consecutive_errors or is_critical_error:
            mapping["unhealthy"] = "true"
            mapping["last_check"] = str(current_time)
            # 标记为不健康

        await self.store.client.hset(health_key, mapping=mapping)

    async def _record_key_success(self, key: KeyItem):
        """记录API密钥成功，重置错误计数"""
        health_key = f"health:{key.client_id}"
        current_time = int(time.time())

        # 重置错误计数和健康状态
        await self.store.client.hset(health_key, mapping={
            "consecutive_errors": "0",
            "unhealthy": "false",
            "last_success": str(current_time)
        })

    async def _fetch_new_token(self, key: KeyItem) -> Tuple[str, Optional[int]]:
        params = {
            "grant_type": "client_credentials",
            "client_id": key.client_id,
            "client_secret": key.client_secret,
        }
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(settings.baidu_token_url, params=params, headers={"Accept": "application/json"})

            if r.status_code != 200:
                error_msg = f"HTTP {r.status_code}: {r.text}"
                await self._record_key_error(key, error_msg)
                await self.store.client.incrby("metrics:upstream_errors_total", 1)
                raise HTTPException(status_code=502, detail=f"获取 token 失败: {error_msg}")

            data = r.json()
            access_token = data.get("access_token")
            expires_in = data.get("expires_in")  # 秒

            if not access_token:
                error_msg = f"token 响应异常: {data}"
                await self._record_key_error(key, error_msg)
                raise HTTPException(status_code=502, detail=error_msg)

            # 记录成功
            await self._record_key_success(key)
            return access_token, int(expires_in) if expires_in else None

        except httpx.RequestError as e:
            error_msg = f"网络错误: {str(e)}"
            await self._record_key_error(key, error_msg)
            raise HTTPException(status_code=502, detail=f"获取 token 失败: {error_msg}")
        except Exception as e:
            error_msg = f"未知错误: {str(e)}"
            await self._record_key_error(key, error_msg)
            raise HTTPException(status_code=502, detail=f"获取 token 失败: {error_msg}")

    async def _save_token(self, key: KeyItem, token: str, ttl_seconds: Optional[int]):
        expire_ts = int(time.time()) + (ttl_seconds or 0)
        pipe = self.store.client.pipeline()
        await pipe.hset(f"token:{key.client_id}", mapping={
            "token": token,
            "remaining": self.token_max_uses,
            "expire_ts": expire_ts,
        })
        if ttl_seconds:
            await pipe.expire(
                f"token:{key.client_id}",
                ttl_seconds - 30 if ttl_seconds > 60 else ttl_seconds
            )
        await pipe.execute()

    async def _get_cached(self, key: KeyItem) -> Optional[Dict[str, Any]]:
        data = await self.store.client.hgetall(f"token:{key.client_id}")
        if not data:
            return None
        decoded = {k.decode(): v.decode() for k, v in data.items()}
        remaining = int(decoded.get("remaining", 0))
        expire_ts = int(decoded.get("expire_ts", 0))
        if remaining <= 0:
            return None
        if expire_ts and expire_ts <= int(time.time()):
            return None
        return decoded

    async def _decrement_use(self, key: KeyItem):
        # 防止并发导致remaining变成负数
        pipe = self.store.client.pipeline()
        await pipe.hincrby(f"token:{key.client_id}", "remaining", -1)
        await pipe.hget(f"token:{key.client_id}", "remaining")
        _, remaining_bytes = await pipe.execute()
        
        # 如果变成负数，将其重置为0
        if remaining_bytes and int(remaining_bytes) < 0:
            await self.store.client.hset(f"token:{key.client_id}", mapping={"remaining": "0"})

    async def get_token(self) -> Tuple[str, KeyItem]:
        # 先尽量复用现有可用 token，减少刷新频率
        healthy_keys = await self._get_healthy_keys()
        for key in healthy_keys:
            cached = await self._get_cached(key)
            if cached:
                return cached["token"], key
        
        # 尝试获取新token，按顺序尝试每个健康密钥
        for key in healthy_keys:
            try:
                token, ttl = await self._fetch_new_token(key)
                await self._save_token(key, token, ttl)
                return token, key
            except HTTPException:
                # token获取失败，该密钥已经在_fetch_new_token中被标记为不健康
                # 继续尝试下一个密钥
                continue
        
        # 如果所有健康密钥都失败了，尝试所有密钥（紧急情况）
        remaining_keys = [key for key in self.keys if key not in healthy_keys]
        for key in remaining_keys:
            try:
                token, ttl = await self._fetch_new_token(key)
                await self._save_token(key, token, ttl)
                return token, key
            except HTTPException:
                continue
        
        # 所有密钥都失败
        raise HTTPException(status_code=502, detail="所有API密钥都无法获取token")

    async def _check_monthly_quota(self, key: KeyItem) -> bool:
        """检查月配额是否超限"""
        current_month = time.strftime("%Y-%m")
        monthly_key = f"monthly:{key.client_id}:{current_month}"
        current_usage = await self.store.client.get(monthly_key)
        current_usage = int(current_usage) if current_usage else 0
        return current_usage < self.monthly_quota_limit

    async def _check_qps_limit(self, key: KeyItem) -> bool:
        """检查QPS限制"""
        current_second = int(time.time())
        qps_key = f"qps:{key.client_id}:{current_second}"
        current_qps = await self.store.client.get(qps_key)
        current_qps = int(current_qps) if current_qps else 0
        return current_qps < self.qps_limit

    async def _increment_monthly_usage(self, key: KeyItem):
        """增加月度使用计数"""
        current_month = time.strftime("%Y-%m")
        monthly_key = f"monthly:{key.client_id}:{current_month}"
        pipe = self.store.client.pipeline()
        await pipe.incr(monthly_key)
        # 设置过期时间为下个月初（使用UTC时间避免时区问题）
        now = datetime.datetime.now(datetime.timezone.utc)
        year = now.year + (1 if now.month == 12 else 0)
        month = 1 if now.month == 12 else now.month + 1
        expire_at = calendar.timegm(datetime.datetime(year, month, 1).timetuple())
        await pipe.expireat(monthly_key, expire_at)
        await pipe.execute()

    async def _increment_qps_usage(self, key: KeyItem):
        """增加QPS计数"""
        current_second = int(time.time())
        qps_key = f"qps:{key.client_id}:{current_second}"
        pipe = self.store.client.pipeline()
        await pipe.incr(qps_key)
        await pipe.expire(qps_key, 2)  # 2秒后过期
        await pipe.execute()

    async def consume(self, key: KeyItem):
        """消费一次token使用，包含配额检查"""
        # 检查月配额
        if not await self._check_monthly_quota(key):
            raise HTTPException(status_code=429, detail=f"月配额已用完 (限制: {self.monthly_quota_limit}次/月)")

        # 检查QPS限制
        if not await self._check_qps_limit(key):
            raise HTTPException(status_code=429, detail=f"QPS限制 (限制: {self.qps_limit}次/秒)")

        # 正常消费
        await self._decrement_use(key)
        await self._increment_monthly_usage(key)
        await self._increment_qps_usage(key)


# ---------------------------
# FastAPI 应用
# ---------------------------

store = RedisStore(settings.redis_url, settings.redis_password)
manager = TokenManager(store, settings.baidu_keys, settings.token_max_uses, settings.monthly_quota_limit,
                       settings.qps_limit, settings.max_consecutive_errors, settings.health_check_interval)


# ---------------------------
# API密钥校验
# ---------------------------

def verify_api_key(
        authorization: Optional[str] = Header(None),
        x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        api_key: Optional[str] = Header(None, alias="API-Key")
):
    """
    API密钥校验依赖函数
    支持三种方式传递API密钥：
    1. Header: Authorization: Bearer <API_KEY> (推荐)
    2. Header: X-API-Key: <API_KEY>
    3. Header: API-Key: <API_KEY>
    """
    if not settings.api_key:
        # 如果未配置API_KEY，则不进行校验
        return True

    provided_key = None

    # 检查 Authorization: Bearer <token>
    if authorization and authorization.startswith("Bearer "):
        provided_key = authorization[7:]  # 移除 "Bearer " 前缀

    # 检查其他header方式
    if not provided_key:
        provided_key = x_api_key or api_key

    if not provided_key:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "missing_api_key",
                "message": "缺少API密钥"
            }
        )

    if provided_key != settings.api_key:
        raise HTTPException(
            status_code=401,
            detail={
                "error": "invalid_api_key",
                "message": "API密钥无效",
                "hint": "请检查您的API密钥是否正确"
            }
        )

    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    print("=" * 50)
    print("百度OCR API代理服务启动中...")
    print("=" * 50)
    print("环境变量加载状态:")
    print(f"   BAIDU_KEYS: {len(settings.baidu_keys)} 个密钥已加载")
    print(f"   REDIS_URL: {settings.redis_url}")
    print(f"   TOKEN_MAX_USES: {settings.token_max_uses}")
    print(f"   MONTHLY_QUOTA_LIMIT: {settings.monthly_quota_limit}")
    print(f"   QPS_LIMIT: {settings.qps_limit}")
    print(f"   MAX_CONSECUTIVE_ERRORS: {settings.max_consecutive_errors}")
    print(f"   HEALTH_CHECK_INTERVAL: {settings.health_check_interval}秒")

    # API密钥状态
    if settings.api_key:
        print(f"   API_KEY: 已设置 ({settings.api_key[:3]}***{settings.api_key[-3:]})")
        print("   安全模式: API密钥校验已启用")
    else:
        print("   API_KEY: 未设置")
        print("   安全模式: 无API密钥校验 (任何人都可访问)")

    print(f"   BAIDU_TOKEN_URL: {settings.baidu_token_url}")
    print(f"   BAIDU_OCR_URL: {settings.baidu_ocr_url}")
    if hasattr(settings, 'baidu_general_ocr_url'):
        print(f"   BAIDU_GENERAL_OCR_URL: {settings.baidu_general_ocr_url}")
    print("=" * 50)

    try:
        await store.client.ping()
        print("[startup] Redis 连接成功")

        # 清理 Redis 里的 token 缓存和轮询索引 - 这块不需要的时候可以注释掉
        async for key in store.client.scan_iter("token:*"):
            await store.client.delete(key)
        await store.client.delete("rr:index")
        print("[startup] 已清理 Redis 中的 token 缓存")

    except Exception as e:
        print(f"[startup] Redis 连接失败: {e}")

    yield

    # Shutdown
    await store.close()


app = FastAPI(title="百度 AIP 代理服务", version="1.0", lifespan=lifespan)


# ---------------------------
# Pydantic 模型
# ---------------------------

class HealthResponse(BaseModel):
    status: str
    redis_ok: bool
    keys_loaded: int


class OCRUploadRequest(BaseModel):
    """
    支持百度OCR API的所有参数的请求模型
    参数优先级：image > url > pdf_file > ofd_file
    """
    # 四选一的主要参数
    image: Optional[str] = None  # base64编码的图片
    url: Optional[str] = None  # 图片URL
    pdf_file: Optional[str] = None  # base64编码的PDF
    ofd_file: Optional[str] = None  # base64编码的OFD

    # 页码参数
    pdf_file_num: Optional[str] = None  # PDF页码，默认第1页
    ofd_file_num: Optional[str] = None  # OFD页码，默认第1页

    # 功能参数
    verify_parameter: Optional[str] = None  # 是否开启验真 true/false
    probability: Optional[str] = None  # 是否返回置信度 true/false
    location: Optional[str] = None  # 是否返回坐标 true/false

    @classmethod
    def _validate_bool_string(cls, v):
        if v is not None and v not in ["true", "false"]:
            raise ValueError("参数值必须为 'true' 或 'false'")
        return v

    @classmethod
    def _validate_page_num(cls, v):
        if v is not None:
            try:
                page_num = int(v)
                if page_num < 1:
                    raise ValueError("页码必须大于0")
            except ValueError:
                raise ValueError("页码必须为正整数")
        return v


class OCRResponse(BaseModel):
    baidu_raw: Dict[str, Any]
    used_key: str
    remaining_estimate: Optional[int] = None


# ---------------------------
# 接口
# ---------------------------


#  手动清理Token缓存接口
@app.post("/clear_tokens", dependencies=[Depends(verify_api_key)])
async def clear_tokens():
    deleted = 0
    async for key in store.client.scan_iter("token:*"):
        deleted += await store.client.delete(key)
    await store.client.delete("rr:index")
    await store.client.delete("rr:healthy_index")
    return {"status": "ok", "deleted": deleted}


@app.get("/health", response_model=HealthResponse)
async def health():
    redis_ok = True
    try:
        await store.client.ping()
    except Exception:
        redis_ok = False
    return HealthResponse(
        status="ok" if redis_ok and bool(settings.baidu_keys) else "degraded",
        redis_ok=redis_ok,
        keys_loaded=len(settings.baidu_keys),
    )


@app.post("/token/refresh")
async def token_refresh(api_key_valid: bool = Depends(verify_api_key)):
    # 使用和get_token相同的逻辑，自动跳过不健康的密钥
    token, key = await manager.get_token()
    return {
        "client_id": key.client_id,
        "access_token": token,
        "expires_in": None,  # get_token返回的token可能是缓存的，没有TTL信息
        "remaining": settings.token_max_uses,
    }


@app.get("/token/state")
async def token_state(api_key_valid: bool = Depends(verify_api_key)):
    items = []
    now = int(time.time())
    for key in settings.baidu_keys:
        data = await store.client.hgetall(f"token:{key.client_id}")
        if data:
            d = {k.decode(): v.decode() for k, v in data.items()}
            d["client_id"] = key.client_id
            if "expire_ts" in d:
                d["time_left_s"] = max(0, int(d["expire_ts"]) - now)
            items.append(d)
        else:
            items.append({
                "client_id": key.client_id,
                "token": None,
                "remaining": 0,
                "time_left_s": 0,
            })
    return {"tokens": items}


@app.get("/quota/status")
async def quota_status(api_key_valid: bool = Depends(verify_api_key)):
    """查看配额使用情况"""
    current_month = time.strftime("%Y-%m")
    quota_info = []

    for key in settings.baidu_keys:
        # 获取月度使用量
        monthly_key = f"monthly:{key.client_id}:{current_month}"
        monthly_usage = await store.client.get(monthly_key)
        monthly_usage = int(monthly_usage) if monthly_usage else 0

        # 获取当前QPS
        current_second = int(time.time())
        qps_key = f"qps:{key.client_id}:{current_second}"
        current_qps = await store.client.get(qps_key)
        current_qps = int(current_qps) if current_qps else 0

        # 获取token状态
        token_data = await store.client.hgetall(f"token:{key.client_id}")
        if token_data:
            decoded = {k.decode(): v.decode() for k, v in token_data.items()}
            remaining_uses = int(decoded.get("remaining", 0))
            expire_ts = int(decoded.get("expire_ts", 0))
            days_left = max(0, (expire_ts - int(time.time())) // (24 * 3600)) if expire_ts else 0
        else:
            remaining_uses = 0
            days_left = 0

        # 获取健康状态
        health_data = await store.client.hgetall(f"health:{key.client_id}")
        if health_data:
            health_decoded = {k.decode(): v.decode() for k, v in health_data.items()}
            is_healthy = health_decoded.get("unhealthy", "false") != "true"
            consecutive_errors = int(health_decoded.get("consecutive_errors", 0))
            last_error = health_decoded.get("last_error", "")
            last_success = health_decoded.get("last_success", "")
        else:
            is_healthy = True
            consecutive_errors = 0
            last_error = ""
            last_success = ""

        quota_info.append({
            "client_id": key.client_id,
            "monthly_usage": monthly_usage,
            "monthly_limit": settings.monthly_quota_limit,
            "monthly_remaining": settings.monthly_quota_limit - monthly_usage,
            "current_qps": current_qps,
            "qps_limit": settings.qps_limit,
            "token_remaining_uses": remaining_uses,
            "token_max_uses": settings.token_max_uses,
            "token_days_left": days_left,
            "health": {
                "is_healthy": is_healthy,
                "consecutive_errors": consecutive_errors,
                "last_error": last_error,
                "last_success": last_success
            },
            "status": {
                "monthly_ok": monthly_usage < settings.monthly_quota_limit,
                "qps_ok": current_qps < settings.qps_limit,
                "token_ok": remaining_uses > 0 and days_left > 0,
                "health_ok": is_healthy
            }
        })

    return {
        "current_month": current_month,
        "quota_details": quota_info,
        "summary": {
            "total_monthly_usage": sum(item["monthly_usage"] for item in quota_info),
            "total_monthly_limit": len(quota_info) * settings.monthly_quota_limit,
            "total_keys": len(quota_info),
            "healthy_keys": sum(1 for item in quota_info if item["health"]["is_healthy"]),
            "unhealthy_keys": sum(1 for item in quota_info if not item["health"]["is_healthy"]),
            "fully_functional_keys": sum(1 for item in quota_info if all(item["status"].values()))
        }
    }


@app.post("/ocr/url", response_model=OCRResponse)
async def ocr_url_recognition(
        url: str = Form(..., description="图片完整URL，长度不超过1024字节"),
        verify_parameter: Optional[str] = Form(None, description="是否开启验真（true/false）"),
        probability: Optional[str] = Form(None, description="是否返回置信度（true/false）"),
        location: Optional[str] = Form(None, description="是否返回坐标（true/false）"),
        api_key_valid: bool = Depends(verify_api_key)
):
    # 验证URL长度
    if len(url) > 1024:
        raise HTTPException(
            status_code=400,
            detail="URL长度超过限制，最大允许1024字节"
        )

    # 获取token
    token, key = await manager.get_token()

    # 构建请求数据
    data = {"url": url}

    # 添加可选参数
    if verify_parameter is not None:
        data["verify_parameter"] = verify_parameter
    if probability is not None:
        data["probability"] = probability
    if location is not None:
        data["location"] = location

    # 调用百度OCR API
    async with httpx.AsyncClient(timeout=30) as client:
        api_url = f"{settings.baidu_ocr_url}?access_token={token}"
        r = await client.post(
            api_url,
            data=data,
            headers={"content-type": "application/x-www-form-urlencoded"}
        )

    await store.client.incrby("metrics:requests_total", 1)

    if r.status_code != 200:
        await store.client.incrby("metrics:upstream_errors_total", 1)
        # 401/400：视为token失效，立即将remaining置0强制下次刷新
        if r.status_code in (400, 401):
            await store.client.hset(f"token:{key.client_id}", mapping={"remaining": 0})
        raise HTTPException(status_code=502, detail=f"上游OCR错误: {r.status_code} {r.text}")

    # 正常扣减一次使用次数
    await manager.consume(key)

    raw = r.json()
    remaining = await store.client.hget(f"token:{key.client_id}", "remaining")
    remaining_int: Optional[int] = int(remaining) if remaining is not None else None

    return OCRResponse(baidu_raw=raw, used_key=key.client_id, remaining_estimate=remaining_int)


@app.post("/ocr/upload", response_model=OCRResponse)
async def ocr_upload_file(file: UploadFile = File(...), api_key_valid: bool = Depends(verify_api_key)):
    allowed_types = {
        'image/jpeg', 'image/jpg', 'image/png', 'image/bmp',
        'image/gif', 'image/webp', 'application/pdf'
    }

    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {file.content_type}。支持的类型: {', '.join(allowed_types)}"
        )

    # 检查文件大小 (限制为 4MB，符合百度API要求)
    max_size = 4 * 1024 * 1024  # 4MB
    file_content = await file.read()

    if len(file_content) > max_size:
        raise HTTPException(
            status_code=400,
            detail=f"文件大小超过限制。最大允许: {max_size // (1024 * 1024)}MB，当前文件: {len(file_content) // (1024 * 1024)}MB"
        )

    # 获取 token
    token, key = await manager.get_token()

    # 准备数据 - 根据文件类型使用不同参数
    if file.content_type == 'application/pdf':
        # PDF文件使用 pdf_file 参数
        data = {
            "pdf_file": base64.b64encode(file_content).decode(),
        }
    else:
        # 图片文件使用 image 参数
        data = {
            "image": base64.b64encode(file_content).decode(),
        }

    # 调用百度 OCR API
    async with httpx.AsyncClient(timeout=30) as client:
        url = f"{settings.baidu_ocr_url}?access_token={token}"
        r = await client.post(
            url, data=data, headers={"content-type": "application/x-www-form-urlencoded"}
        )

    await store.client.incrby("metrics:requests_total", 1)

    if r.status_code != 200:
        await store.client.incrby("metrics:upstream_errors_total", 1)
        # 401/400：视为 token 失效，立即将 remaining 置 0 强制下次刷新
        if r.status_code in (400, 401):
            await store.client.hset(f"token:{key.client_id}", mapping={"remaining": 0})
        raise HTTPException(status_code=502, detail=f"上游 OCR 错误: {r.status_code} {r.text}")

    # 正常扣减一次使用次数
    await manager.consume(key)

    raw = r.json()
    remaining = await store.client.hget(f"token:{key.client_id}", "remaining")
    remaining_int: Optional[int] = int(remaining) if remaining is not None else None

    return OCRResponse(baidu_raw=raw, used_key=key.client_id, remaining_estimate=remaining_int)


@app.post("/ocr/upload_smart", response_model=OCRResponse)
async def ocr_upload_smart(
        file: Optional[UploadFile] = File(None),
        image: Optional[str] = Form(None),
        url: Optional[str] = Form(None),
        pdf_file: Optional[str] = Form(None),
        ofd_file: Optional[str] = Form(None),
        pdf_file_num: Optional[str] = Form(None),
        ofd_file_num: Optional[str] = Form(None),
        verify_parameter: Optional[str] = Form(None),
        probability: Optional[str] = Form(None),
        location: Optional[str] = Form(None),
        api_key_valid: bool = Depends(verify_api_key)
):
    # 构建请求数据
    data = {}

    # 处理文件上传
    if file is not None:
        # 检查文件类型和大小
        allowed_types = {
            'image/jpeg', 'image/jpg', 'image/png', 'image/bmp',
            'image/gif', 'image/webp', 'application/pdf', 'application/ofd'
        }

        if file.content_type not in allowed_types:
            raise HTTPException(
                status_code=400,
                detail=f"不支持的文件类型: {file.content_type}。支持的类型: {', '.join(allowed_types)}"
            )

        # 检查文件大小 (限制为 4MB，符合百度API要求)
        max_size = 4 * 1024 * 1024  # 4MB
        file_content = await file.read()

        if len(file_content) > max_size:
            raise HTTPException(
                status_code=400,
                detail=f"文件大小超过限制。最大允许: {max_size // (1024 * 1024)}MB，当前文件: {len(file_content) // (1024 * 1024)}MB"
            )

        # 根据文件类型设置对应参数
        file_base64 = base64.b64encode(file_content).decode()

        if file.content_type == 'application/pdf':
            data["pdf_file"] = file_base64
        elif file.content_type == 'application/ofd':
            data["ofd_file"] = file_base64
        else:  # 图片类型
            data["image"] = file_base64

    # 处理表单参数（按优先级）
    # 优先级：image > url > pdf_file > ofd_file
    if image:
        data["image"] = image
    elif url and "image" not in data:
        data["url"] = url
    elif pdf_file and "image" not in data and "url" not in data:
        data["pdf_file"] = pdf_file
    elif ofd_file and "image" not in data and "url" not in data and "pdf_file" not in data:
        data["ofd_file"] = ofd_file

    # 添加页码参数
    if pdf_file_num and ("pdf_file" in data):
        data["pdf_file_num"] = pdf_file_num
    if ofd_file_num and ("ofd_file" in data):
        data["ofd_file_num"] = ofd_file_num

    # 添加功能参数
    if verify_parameter in ["true", "false"]:
        data["verify_parameter"] = verify_parameter
    if probability in ["true", "false"]:
        data["probability"] = probability
    if location in ["true", "false"]:
        data["location"] = location

    # 验证至少有一个主要参数
    main_params = ["image", "url", "pdf_file", "ofd_file"]
    if not any(param in data for param in main_params):
        raise HTTPException(
            status_code=400,
            detail="需要提供以下参数之一：文件上传、image、url、pdf_file、ofd_file"
        )

    # 获取 token
    token, key = await manager.get_token()

    # 调用百度 OCR API
    async with httpx.AsyncClient(timeout=30) as client:
        url_endpoint = f"{settings.baidu_ocr_url}?access_token={token}"
        r = await client.post(
            url_endpoint, data=data, headers={"content-type": "application/x-www-form-urlencoded"}
        )

    await store.client.incrby("metrics:requests_total", 1)

    if r.status_code != 200:
        await store.client.incrby("metrics:upstream_errors_total", 1)
        # 401/400：视为 token 失效，立即将 remaining 置 0 强制下次刷新
        if r.status_code in (400, 401):
            await store.client.hset(f"token:{key.client_id}", mapping={"remaining": 0})
        raise HTTPException(status_code=502, detail=f"上游 OCR 错误: {r.status_code} {r.text}")

    # 正常扣减一次使用次数
    await manager.consume(key)

    raw = r.json()
    remaining = await store.client.hget(f"token:{key.client_id}", "remaining")
    remaining_int: Optional[int] = int(remaining) if remaining is not None else None

    return OCRResponse(baidu_raw=raw, used_key=key.client_id, remaining_estimate=remaining_int)


@app.get("/")
async def root():
    return {
        "name": "百度 AIP 代理服务",
        "version": "1.3.0",
        "security": "API密钥校验已启用" if settings.api_key else "无安全校验",
        "endpoints": [
            "GET /health",
            "GET /token/state 🔒",
            "POST /token/refresh 🔒",
            "GET /quota/status 🔒",
            "POST /ocr/url 🔒",
            "POST /ocr/upload 🔒",
            "POST /ocr/upload_smart 🔒",
        ],
        "description": {
            "/health": "健康检查（无需API密钥）",
            "/ocr/url": "票据URL识别 - 支持13种常见票据（增值税发票、火车票、出租车票、飞机行程单等）的分类及结构化识别，支持混贴场景",
            "/ocr/upload": "直接上传图片或PDF文件进行OCR识别（支持jpg、png、pdf等格式）",
            "/ocr/upload_smart": "智能OCR接口 - 支持百度API所有参数（image/url/pdf_file/ofd_file + 验真/置信度/坐标等）",
            "/quota/status": "查看月配额、QPS和Token使用情况"
        },
        "auth_info": {
            "required": bool(settings.api_key),
            "method": "Header",
            "headers": ["Authorization: Bearer <API_KEY>", "X-API-Key", "API-Key"],
            "examples": {
                "bearer": "curl -H 'Authorization: Bearer 123456' http://localhost:8000/quota/status",
                "x_api_key": "curl -H 'X-API-Key: 123456' http://localhost:8000/quota/status",
                "api_key": "curl -H 'API-Key: 123456' http://localhost:8000/quota/status"
            }
        }
    }


# ---------------------------
# 本地调试入口
# ---------------------------

if __name__ == "__main__":
    import uvicorn

    # uvicorn.run("baidu_api:app", host="127.0.0.1", port=8000, reload=True)
    uvicorn.run(app, host="0.0.0.0", port=8182)
