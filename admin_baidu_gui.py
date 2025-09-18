import time
import datetime
from typing import Dict, List, Optional, Any
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import uvicorn

# 导入原有的配置
from baidu_api import Settings, RedisStore, KeyItem

# 初始化配置
settings = Settings()
store = RedisStore(settings.redis_url, settings.redis_password)

app = FastAPI(title="百度OCR API管理面板", description="Redis数据管理界面")

# 数据模型
class TokenInfo(BaseModel):
    client_id: str
    token: Optional[str] = None
    remaining: int = 0
    expire_ts: Optional[int] = None
    expire_time: Optional[str] = None
    is_expired: bool = False

class KeyHealthInfo(BaseModel):
    client_id: str
    consecutive_errors: int = 0
    last_error_time: Optional[int] = None
    is_healthy: bool = True
    next_retry_time: Optional[str] = None

class MonthlyUsageInfo(BaseModel):
    client_id: str
    month: str
    usage_count: int = 0
    quota_limit: int = 1000
    usage_percentage: float = 0.0

class SystemStats(BaseModel):
    total_keys: int
    healthy_keys: int
    unhealthy_keys: int
    total_tokens: int
    active_tokens: int
    expired_tokens: int
    total_monthly_usage: int

# API路由
@app.get("/", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """管理面板主页"""
    return HTMLResponse(content=get_dashboard_html(), status_code=200)

@app.post("/api/login")
async def login(request: Request):
    """管理面板登录验证"""
    try:
        body = await request.json()
        password = body.get("password", "")
        
        # 验证密码（使用与baidu_api相同的API_KEY）
        if password == settings.api_key:
            return {"success": True, "message": "登录成功"}
        else:
            return {"success": False, "message": "密码错误"}
    except Exception as e:
        return {"success": False, "message": f"登录失败: {str(e)}"}

@app.get("/api/system/stats")
async def get_system_stats() -> SystemStats:
    """获取系统统计信息"""
    try:
        # 获取所有token信息
        tokens = await get_all_tokens()
        
        # 获取健康状态信息
        health_info = await get_all_health_info()
        
        # 获取月度使用情况
        monthly_usage = await get_all_monthly_usage()
        
        # 计算统计数据
        total_keys = len(settings.baidu_keys)
        healthy_keys = sum(1 for h in health_info if h.is_healthy)
        unhealthy_keys = total_keys - healthy_keys
        
        active_tokens = sum(1 for t in tokens if not t.is_expired and t.token)
        expired_tokens = sum(1 for t in tokens if t.is_expired)
        
        total_monthly_usage = sum(m.usage_count for m in monthly_usage)
        
        return SystemStats(
            total_keys=total_keys,
            healthy_keys=healthy_keys,
            unhealthy_keys=unhealthy_keys,
            total_tokens=len(tokens),
            active_tokens=active_tokens,
            expired_tokens=expired_tokens,
            total_monthly_usage=total_monthly_usage
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取系统统计失败: {str(e)}")

@app.get("/api/tokens")
async def get_tokens() -> List[TokenInfo]:
    """获取所有token信息"""
    return await get_all_tokens()

@app.get("/api/health")
async def get_health() -> List[KeyHealthInfo]:
    """获取所有密钥健康状态"""
    return await get_all_health_info()

@app.get("/api/monthly-usage")
async def get_monthly_usage() -> List[MonthlyUsageInfo]:
    """获取月度使用情况"""
    return await get_all_monthly_usage()

@app.post("/api/tokens/{client_id}/refresh")
async def refresh_token(client_id: str):
    """刷新指定密钥的token"""
    try:
        # 找到对应的密钥
        target_key = None
        for key in settings.baidu_keys:
            if key.client_id == client_id:
                target_key = key
                break
        
        if not target_key:
            raise HTTPException(status_code=404, detail=f"未找到客户端ID: {client_id}")
        
        # 删除现有token
        await store.client.delete(f"token:{client_id}")
        
        # 创建TokenManager实例来获取新token
        from baidu_api import TokenManager
        manager = TokenManager(
            store, settings.baidu_keys, settings.token_max_uses,
            settings.monthly_quota_limit, settings.qps_limit,
            settings.max_consecutive_errors, settings.health_check_interval
        )
        
        # 尝试获取新token
        try:
            new_token, used_key = await manager._fetch_new_token(target_key)
            await manager._save_token(target_key, new_token, 2592000)  # 30天有效期
            return {"message": f"Token {client_id[:8]}... 刷新成功", "success": True}
        except Exception as token_error:
            return {"message": f"Token {client_id[:8]}... 刷新失败: {str(token_error)}", "success": False}
            
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新token失败: {str(e)}")

@app.post("/api/tokens/clear-all")
async def clear_all_tokens():
    """清除所有token"""
    try:
        keys = await store.client.keys("token:*")
        if keys:
            await store.client.delete(*keys)
        return {"message": f"已清除 {len(keys)} 个token"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清除token失败: {str(e)}")

@app.post("/api/health/{client_id}/reset")
async def reset_health(client_id: str):
    """重置指定密钥的健康状态"""
    try:
        await store.client.delete(f"health:{client_id}")
        return {"message": f"密钥 {client_id} 健康状态已重置"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重置健康状态失败: {str(e)}")

@app.post("/api/monthly-usage/clear")
async def clear_monthly_usage():
    """清除所有月度使用记录"""
    try:
        keys = await store.client.keys("monthly:*")
        if keys:
            await store.client.delete(*keys)
        return {"message": f"已清除 {len(keys)} 个月度使用记录"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清除月度使用记录失败: {str(e)}")

@app.post("/api/cleanup/orphaned-data")
async def cleanup_orphaned_data():
    """清理已删除密钥的残留数据"""
    try:
        current_client_ids = {key.client_id for key in settings.baidu_keys}
        cleaned_count = 0
        
        # 清理token数据
        token_keys = await store.client.keys("token:*")
        for token_key_bytes in token_keys:
            token_key = token_key_bytes.decode() if isinstance(token_key_bytes, bytes) else token_key_bytes
            client_id = token_key.replace("token:", "")
            if client_id not in current_client_ids:
                await store.client.delete(token_key)
                cleaned_count += 1
        
        # 清理健康状态数据
        health_keys = await store.client.keys("health:*")
        for health_key_bytes in health_keys:
            health_key = health_key_bytes.decode() if isinstance(health_key_bytes, bytes) else health_key_bytes
            client_id = health_key.replace("health:", "")
            if client_id not in current_client_ids:
                await store.client.delete(health_key)
                cleaned_count += 1
        
        # 清理月度使用数据
        monthly_keys = await store.client.keys("monthly:*")
        for monthly_key_bytes in monthly_keys:
            monthly_key = monthly_key_bytes.decode() if isinstance(monthly_key_bytes, bytes) else monthly_key_bytes
            # monthly:client_id:YYYY-MM
            parts = monthly_key.split(":")
            if len(parts) >= 2:
                client_id = parts[1]
                if client_id not in current_client_ids:
                    await store.client.delete(monthly_key)
                    cleaned_count += 1
        
        return {"message": f"已清理 {cleaned_count} 个孤立数据项"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"清理孤立数据失败: {str(e)}")

# 辅助函数
async def get_all_tokens() -> List[TokenInfo]:
    """获取所有token信息"""
    tokens = []
    current_time = int(time.time())
    
    # 获取Redis中所有token键
    token_keys = await store.client.keys("token:*")
    processed_client_ids = set()
    
    # 处理Redis中存在的token数据
    for token_key_bytes in token_keys:
        token_key = token_key_bytes.decode() if isinstance(token_key_bytes, bytes) else token_key_bytes
        client_id = token_key.replace("token:", "")
        processed_client_ids.add(client_id)
        
        token_data = await store.client.hgetall(token_key)
        
        if token_data:
            expire_ts = int(token_data.get(b'expire_ts', 0))
            expire_time = datetime.datetime.fromtimestamp(expire_ts).strftime('%Y-%m-%d %H:%M:%S') if expire_ts else None
            is_expired = expire_ts < current_time if expire_ts else True
            
            # 检查是否是当前配置中的密钥
            is_current_key = any(key.client_id == client_id for key in settings.baidu_keys)
            
            tokens.append(TokenInfo(
                client_id=client_id + (" [已删除]" if not is_current_key else ""),
                token=token_data.get(b'token', b'').decode() if token_data.get(b'token') else None,
                remaining=int(token_data.get(b'remaining', 0)),
                expire_ts=expire_ts if expire_ts else None,
                expire_time=expire_time,
                is_expired=is_expired
            ))
    
    # 处理当前配置中但Redis中没有token数据的密钥
    for key in settings.baidu_keys:
        if key.client_id not in processed_client_ids:
            tokens.append(TokenInfo(client_id=key.client_id))
    
    return tokens

async def get_all_health_info() -> List[KeyHealthInfo]:
    """获取所有密钥健康状态"""
    health_info = []
    current_time = int(time.time())
    
    # 获取Redis中所有健康状态键
    health_keys = await store.client.keys("health:*")
    processed_client_ids = set()
    
    # 处理Redis中存在的健康状态数据
    for health_key_bytes in health_keys:
        health_key = health_key_bytes.decode() if isinstance(health_key_bytes, bytes) else health_key_bytes
        client_id = health_key.replace("health:", "")
        processed_client_ids.add(client_id)
        
        health_data = await store.client.hgetall(health_key)
        
        consecutive_errors = 0
        last_error_time = None
        is_healthy = True
        next_retry_time = None
        
        if health_data:
            consecutive_errors = int(health_data.get(b'consecutive_errors', 0))
            last_error_time = int(health_data.get(b'last_error_time', 0))
            unhealthy_flag = health_data.get(b'unhealthy', b'false').decode().lower()
            
            # 检查健康状态：要么连续错误超限，要么被明确标记为不健康
            if consecutive_errors >= settings.max_consecutive_errors or unhealthy_flag == 'true':
                is_healthy = False
                next_retry = last_error_time + settings.health_check_interval
                if next_retry > current_time:
                    next_retry_time = datetime.datetime.fromtimestamp(next_retry).strftime('%Y-%m-%d %H:%M:%S')
        
        # 检查是否是当前配置中的密钥
        is_current_key = any(key.client_id == client_id for key in settings.baidu_keys)
        
        health_info.append(KeyHealthInfo(
            client_id=client_id + (" [已删除]" if not is_current_key else ""),
            consecutive_errors=consecutive_errors,
            last_error_time=last_error_time,
            is_healthy=is_healthy,
            next_retry_time=next_retry_time
        ))
    
    # 处理当前配置中但Redis中没有健康状态数据的密钥
    for key in settings.baidu_keys:
        if key.client_id not in processed_client_ids:
            health_info.append(KeyHealthInfo(
                client_id=key.client_id,
                consecutive_errors=0,
                last_error_time=None,
                is_healthy=True,
                next_retry_time=None
            ))
    
    return health_info

async def get_all_monthly_usage() -> List[MonthlyUsageInfo]:
    """获取所有月度使用情况"""
    usage_info = []
    current_month = time.strftime("%Y-%m")
    
    for key in settings.baidu_keys:
        monthly_key = f"monthly:{key.client_id}:{current_month}"
        usage_count = await store.client.get(monthly_key)
        usage_count = int(usage_count) if usage_count else 0
        
        usage_percentage = (usage_count / settings.monthly_quota_limit) * 100 if settings.monthly_quota_limit > 0 else 0
        
        usage_info.append(MonthlyUsageInfo(
            client_id=key.client_id,
            month=current_month,
            usage_count=usage_count,
            quota_limit=settings.monthly_quota_limit,
            usage_percentage=round(usage_percentage, 1)
        ))
    
    return usage_info

def get_dashboard_html() -> str:
    """返回管理面板HTML"""
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>百度OCR API管理面板</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: #333;
        }
        
        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }
        
        .header {
            text-align: center;
            margin-bottom: 30px;
            color: white;
        }
        
        .header h1 {
            font-size: 2.5rem;
            margin-bottom: 10px;
            text-shadow: 0 2px 4px rgba(0,0,0,0.3);
        }
        
        .header p {
            font-size: 1.1rem;
            opacity: 0.9;
        }
        
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }
        
        .stat-card {
            background: white;
            padding: 20px;
            border-radius: 12px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            text-align: center;
            transition: transform 0.2s;
        }
        
        .stat-card:hover {
            transform: translateY(-2px);
        }
        
        .stat-number {
            font-size: 2rem;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 5px;
        }
        
        .stat-label {
            color: #666;
            font-size: 0.9rem;
        }
        
        .tabs {
            display: flex;
            background: white;
            border-radius: 12px;
            padding: 5px;
            margin-bottom: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        
        .tab {
            flex: 1;
            padding: 12px 20px;
            text-align: center;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s;
            font-weight: 500;
        }
        
        .tab.active {
            background: #667eea;
            color: white;
        }
        
        .tab:hover:not(.active) {
            background: #f5f5f5;
        }
        
        .content-panel {
            background: white;
            border-radius: 12px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            display: none;
        }
        
        .content-panel.active {
            display: block;
        }
        
        .table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
        }
        
        .table th,
        .table td {
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }
        
        .table th {
            background: #f8f9fa;
            font-weight: 600;
            color: #555;
        }
        
        .status-badge {
            padding: 4px 8px;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 500;
        }
        
        .status-healthy {
            background: #d4edda;
            color: #155724;
        }
        
        .status-unhealthy {
            background: #f8d7da;
            color: #721c24;
        }
        
        .status-active {
            background: #d1ecf1;
            color: #0c5460;
        }
        
        .status-expired {
            background: #fff3cd;
            color: #856404;
        }
        
        .progress-bar {
            width: 100%;
            height: 8px;
            background: #e9ecef;
            border-radius: 4px;
            overflow: hidden;
        }
        
        .progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #28a745, #ffc107, #dc3545);
            transition: width 0.3s;
        }
        
        .btn {
            padding: 8px 16px;
            border: none;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.9rem;
            transition: all 0.2s;
            margin: 2px;
        }
        
        .btn-primary {
            background: #667eea;
            color: white;
        }
        
        .btn-primary:hover {
            background: #5a6fd8;
        }
        
        .btn-danger {
            background: #dc3545;
            color: white;
        }
        
        .btn-danger:hover {
            background: #c82333;
        }
        
        .btn-success {
            background: #28a745;
            color: white;
        }
        
        .btn-success:hover {
            background: #218838;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
            color: #666;
        }
        
        .error {
            background: #f8d7da;
            color: #721c24;
            padding: 15px;
            border-radius: 8px;
            margin: 10px 0;
        }
        
        .success {
            background: #d4edda;
            color: #155724;
            padding: 15px;
            border-radius: 8px;
            margin: 10px 0;
        }
        
        .login-container {
            display: flex;
            justify-content: center;
            align-items: center;
            min-height: 100vh;
            padding: 20px;
        }
        
        .login-box {
            background: white;
            padding: 40px;
            border-radius: 16px;
            box-shadow: 0 8px 32px rgba(0,0,0,0.1);
            text-align: center;
            max-width: 400px;
            width: 100%;
        }
        
        .login-box h2 {
            color: #667eea;
            margin-bottom: 10px;
            font-size: 1.8rem;
        }
        
        .login-box p {
            color: #666;
            margin-bottom: 30px;
        }
        
        .login-form {
            display: flex;
            flex-direction: column;
            gap: 15px;
        }
        
        .login-form input {
            padding: 12px 16px;
            border: 2px solid #e9ecef;
            border-radius: 8px;
            font-size: 1rem;
            transition: border-color 0.2s;
        }
        
        .login-form input:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .logout-btn {
            position: absolute;
            top: 20px;
            right: 20px;
            font-size: 0.9rem;
        }
        
        @media (max-width: 768px) {
            .container {
                padding: 10px;
            }
            
            .header h1 {
                font-size: 2rem;
            }
            
            .tabs {
                flex-direction: column;
            }
            
            .table {
                font-size: 0.9rem;
            }
            
            .logout-btn {
                position: static;
                margin-top: 10px;
            }
        }
    </style>
</head>
<body>
    <!-- 登录页面 -->
    <div id="loginPage" class="login-container">
        <div class="login-box">
            <h2>🔐 管理面板登录</h2>
            <p>请输入管理密码访问面板</p>
            <div class="login-form">
                <input type="password" id="loginPassword" placeholder="请输入管理密码" />
                <button class="btn btn-primary" onclick="performLogin()">登录</button>
            </div>
            <div id="loginError" class="error" style="display: none;"></div>
        </div>
    </div>

    <!-- 主面板 -->
    <div id="mainPanel" class="container" style="display: none;">
        <div class="header">
            <h1>🚀 百度OCR API管理面板</h1>
            <button class="btn btn-danger logout-btn" onclick="logout()">退出登录</button>
        </div>
        
        <div class="stats-grid" id="statsGrid">
            <div class="loading">正在加载统计数据...</div>
        </div>
        
        <div class="tabs">
            <div class="tab active" onclick="switchTab('tokens')">Token管理</div>
            <div class="tab" onclick="switchTab('health')">健康状态</div>
            <div class="tab" onclick="switchTab('usage')">使用统计</div>
        </div>
        
        <div id="tokens" class="content-panel active">
            <h3>🔑 Token管理</h3>
            <button class="btn btn-danger" onclick="clearAllTokens()">清除所有Token</button>
            <div id="tokensContent">
                <div class="loading">正在加载Token数据...</div>
            </div>
        </div>
        
        <div id="health" class="content-panel">
            <h3>💚 健康状态监控</h3>
            <button class="btn btn-danger" onclick="cleanupOrphanedData()">清理孤立数据</button>
            <div id="healthContent">
                <div class="loading">正在加载健康状态数据...</div>
            </div>
        </div>
        
        <div id="usage" class="content-panel">
            <h3>📊 月度使用统计</h3>
            <button class="btn btn-danger" onclick="clearMonthlyUsage()">清除月度记录</button>
            <div id="usageContent">
                <div class="loading">正在加载使用统计数据...</div>
            </div>
        </div>
    </div>

    <script>
        let currentTab = 'tokens';
        let isLoggedIn = false;
        
        // 页面加载时初始化
        document.addEventListener('DOMContentLoaded', function() {
            checkLoginStatus();
        });
        
        // 检查登录状态
        function checkLoginStatus() {
            const savedPassword = localStorage.getItem('adminPassword');
            if (savedPassword) {
                // 验证保存的密码
                verifyPassword(savedPassword, true);
            } else {
                showLoginPage();
            }
        }
        
        // 显示登录页面
        function showLoginPage() {
            document.getElementById('loginPage').style.display = 'flex';
            document.getElementById('mainPanel').style.display = 'none';
            isLoggedIn = false;
        }
        
        // 显示主面板
        function showMainPanel() {
            document.getElementById('loginPage').style.display = 'none';
            document.getElementById('mainPanel').style.display = 'block';
            isLoggedIn = true;
            loadAllData();
            // 每30秒自动刷新数据
            setInterval(loadAllData, 30000);
        }
        
        // 执行登录
        async function performLogin() {
            const password = document.getElementById('loginPassword').value;
            if (!password) {
                showLoginError('请输入密码');
                return;
            }
            
            await verifyPassword(password, false);
        }
        
        // 验证密码
        async function verifyPassword(password, isAutoLogin) {
            try {
                const response = await fetch('/api/login', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json'
                    },
                    body: JSON.stringify({ password: password })
                });
                
                const result = await response.json();
                
                if (result.success) {
                    // 登录成功，保存密码到localStorage
                    localStorage.setItem('adminPassword', password);
                    showMainPanel();
                    if (!isAutoLogin) {
                        showMessage('登录成功', 'success');
                    }
                } else {
                    if (isAutoLogin) {
                        // 自动登录失败，清除保存的密码
                        localStorage.removeItem('adminPassword');
                        showLoginPage();
                    } else {
                        showLoginError(result.message);
                    }
                }
            } catch (error) {
                if (isAutoLogin) {
                    localStorage.removeItem('adminPassword');
                    showLoginPage();
                } else {
                    showLoginError('登录请求失败');
                }
            }
        }
        
        // 显示登录错误
        function showLoginError(message) {
            const errorDiv = document.getElementById('loginError');
            errorDiv.textContent = message;
            errorDiv.style.display = 'block';
            setTimeout(() => {
                errorDiv.style.display = 'none';
            }, 3000);
        }
        
        // 退出登录
        function logout() {
            localStorage.removeItem('adminPassword');
            showLoginPage();
            showMessage('已退出登录', 'success');
        }
        
        // 回车键登录
        document.addEventListener('keypress', function(e) {
            if (e.key === 'Enter' && !isLoggedIn) {
                performLogin();
            }
        });
        
        // 切换标签页
        function switchTab(tabName) {
            // 更新标签样式
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            event.target.classList.add('active');
            
            // 更新内容面板
            document.querySelectorAll('.content-panel').forEach(panel => panel.classList.remove('active'));
            document.getElementById(tabName).classList.add('active');
            
            currentTab = tabName;
        }
        
        // 加载所有数据
        async function loadAllData() {
            await Promise.all([
                loadStats(),
                loadTokens(),
                loadHealth(),
                loadUsage()
            ]);
        }
        
        // 加载统计数据
        async function loadStats() {
            try {
                const response = await fetch('/api/system/stats');
                const stats = await response.json();
                
                document.getElementById('statsGrid').innerHTML = `
                    <div class="stat-card">
                        <div class="stat-number">${stats.total_keys}</div>
                        <div class="stat-label">总密钥数</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${stats.healthy_keys}</div>
                        <div class="stat-label">健康密钥</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${stats.active_tokens}</div>
                        <div class="stat-label">活跃Token</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-number">${stats.total_monthly_usage}</div>
                        <div class="stat-label">本月总使用</div>
                    </div>
                `;
            } catch (error) {
                console.error('加载统计数据失败:', error);
            }
        }
        
        // 加载Token数据
        async function loadTokens() {
            try {
                const response = await fetch('/api/tokens');
                const tokens = await response.json();
                
                let html = `
                    <table class="table">
                        <thead>
                            <tr>
                                <th>客户端ID</th>
                                <th>Token状态</th>
                                <th>剩余使用次数</th>
                                <th>过期时间</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                
                tokens.forEach(token => {
                    const statusClass = token.is_expired ? 'status-expired' : (token.token ? 'status-active' : 'status-unhealthy');
                    const statusText = token.is_expired ? '已过期' : (token.token ? '正常' : '无Token');
                    
                    html += `
                        <tr>
                            <td>${token.client_id.substring(0, 8)}...</td>
                            <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                            <td>${token.remaining}</td>
                            <td>${token.expire_time || '无'}</td>
                            <td>
                                <button class="btn btn-primary" onclick="refreshToken('${token.client_id}')">刷新</button>
                            </td>
                        </tr>
                    `;
                });
                
                html += '</tbody></table>';
                document.getElementById('tokensContent').innerHTML = html;
            } catch (error) {
                document.getElementById('tokensContent').innerHTML = '<div class="error">加载Token数据失败</div>';
            }
        }
        
        // 加载健康状态数据
        async function loadHealth() {
            try {
                const response = await fetch('/api/health');
                const healthData = await response.json();
                
                let html = `
                    <table class="table">
                        <thead>
                            <tr>
                                <th>客户端ID</th>
                                <th>健康状态</th>
                                <th>连续错误次数</th>
                                <th>下次重试时间</th>
                                <th>操作</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                
                healthData.forEach(health => {
                    const statusClass = health.is_healthy ? 'status-healthy' : 'status-unhealthy';
                    const statusText = health.is_healthy ? '健康' : '不健康';
                    
                    html += `
                        <tr>
                            <td>${health.client_id.substring(0, 8)}...</td>
                            <td><span class="status-badge ${statusClass}">${statusText}</span></td>
                            <td>${health.consecutive_errors}</td>
                            <td>${health.next_retry_time || '无'}</td>
                            <td>
                                <button class="btn btn-success" onclick="resetHealth('${health.client_id}')">重置</button>
                            </td>
                        </tr>
                    `;
                });
                
                html += '</tbody></table>';
                document.getElementById('healthContent').innerHTML = html;
            } catch (error) {
                document.getElementById('healthContent').innerHTML = '<div class="error">加载健康状态数据失败</div>';
            }
        }
        
        // 加载使用统计数据
        async function loadUsage() {
            try {
                const response = await fetch('/api/monthly-usage');
                const usageData = await response.json();
                
                let html = `
                    <table class="table">
                        <thead>
                            <tr>
                                <th>客户端ID</th>
                                <th>月份</th>
                                <th>使用次数</th>
                                <th>配额限制</th>
                                <th>使用率</th>
                            </tr>
                        </thead>
                        <tbody>
                `;
                
                usageData.forEach(usage => {
                    const progressColor = usage.usage_percentage > 80 ? '#dc3545' : 
                                        usage.usage_percentage > 60 ? '#ffc107' : '#28a745';
                    
                    html += `
                        <tr>
                            <td>${usage.client_id.substring(0, 8)}...</td>
                            <td>${usage.month}</td>
                            <td>${usage.usage_count}</td>
                            <td>${usage.quota_limit}</td>
                            <td>
                                <div class="progress-bar">
                                    <div class="progress-fill" style="width: ${usage.usage_percentage}%; background: ${progressColor}"></div>
                                </div>
                                <small>${usage.usage_percentage}%</small>
                            </td>
                        </tr>
                    `;
                });
                
                html += '</tbody></table>';
                document.getElementById('usageContent').innerHTML = html;
            } catch (error) {
                document.getElementById('usageContent').innerHTML = '<div class="error">加载使用统计数据失败</div>';
            }
        }
        
        // 刷新单个Token
        async function refreshToken(clientId) {
            try {
                const response = await fetch(`/api/tokens/${clientId}/refresh`, {
                    method: 'POST'
                });
                const result = await response.json();
                
                showMessage(result.message, 'success');
                loadTokens();
            } catch (error) {
                showMessage('刷新Token失败', 'error');
            }
        }
        
        // 清除所有Token
        async function clearAllTokens() {
            if (!confirm('确定要清除所有Token吗？')) return;
            
            try {
                const response = await fetch('/api/tokens/clear-all', {
                    method: 'POST'
                });
                const result = await response.json();
                
                showMessage(result.message, 'success');
                loadTokens();
            } catch (error) {
                showMessage('清除Token失败', 'error');
            }
        }
        
        // 重置健康状态
        async function resetHealth(clientId) {
            try {
                const response = await fetch(`/api/health/${clientId}/reset`, {
                    method: 'POST'
                });
                const result = await response.json();
                
                showMessage(result.message, 'success');
                loadHealth();
            } catch (error) {
                showMessage('重置健康状态失败', 'error');
            }
        }
        
        // 清除月度使用记录
        async function clearMonthlyUsage() {
            if (!confirm('确定要清除所有月度使用记录吗？')) return;
            
            try {
                const response = await fetch('/api/monthly-usage/clear', {
                    method: 'POST'
                });
                const result = await response.json();
                
                showMessage(result.message, 'success');
                loadUsage();
            } catch (error) {
                showMessage('清除月度使用记录失败', 'error');
            }
        }
        
        // 清理孤立数据
        async function cleanupOrphanedData() {
            if (!confirm('确定要清理已删除密钥的孤立数据吗？这将删除不在当前配置中的密钥的所有Redis数据。')) return;
            
            try {
                const response = await fetch('/api/cleanup/orphaned-data', {
                    method: 'POST'
                });
                const result = await response.json();
                
                showMessage(result.message, 'success');
                loadAllData(); // 重新加载所有数据
            } catch (error) {
                showMessage('清理孤立数据失败', 'error');
            }
        }
        
        // 显示消息
        function showMessage(message, type) {
            const messageDiv = document.createElement('div');
            messageDiv.className = type;
            messageDiv.textContent = message;
            
            const container = document.querySelector('.container');
            container.insertBefore(messageDiv, container.firstChild);
            
            setTimeout(() => {
                messageDiv.remove();
            }, 3000);
        }
    </script>
</body>
</html>
    """

if __name__ == "__main__":
    print("启动百度OCR API管理面板...")
    uvicorn.run(app, host="0.0.0.0", port=8181)