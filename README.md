# 🚀 百度OCR API代理服务

<div align="center">

![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-0.104+-green.svg)
![Redis](https://img.shields.io/badge/Redis-5.0+-red.svg)
![License](https://img.shields.io/badge/License-MIT-yellow.svg)

一个功能强大的百度OCR API代理服务，提供智能负载均衡、健康监控、配额管理和现代化管理面板。

[功能特性](#-功能特性) • [快速开始](#-快速开始) • [API文档](#-api文档) • [管理面板](#-管理面板) • [配置说明](#-配置说明)

</div>

---

## 📋 目录

- [功能特性](#-功能特性)
- [快速开始](#-快速开始)
- [环境配置](#-环境配置)
- [API文档](#-api文档)
- [管理面板](#-管理面板)
- [配置说明](#-配置说明)
- [故障排除](#-故障排除)
- [贡献指南](#-贡献指南)

## ✨ 功能特性

### 🔑 智能API密钥管理
- **多密钥轮询**: 支持多个百度API密钥自动轮询使用
- **健康监控**: 实时监控密钥健康状态，自动跳过异常密钥
- **故障恢复**: 智能错误检测和自动恢复机制
- **负载均衡**: 均匀分配请求负载，避免单点过载

### 📊 配额与限流管理
- **月度配额**: 精确跟踪每个密钥的月度使用量
- **QPS限制**: 支持每秒查询次数限制，符合百度API规范
- **Token管理**: 智能Token缓存和自动刷新机制
- **并发安全**: 支持高并发场景下的安全使用

### 🌐 RESTful API接口
- **文件上传**: 支持图片和PDF文件直接上传识别
- **URL识别**: 支持通过图片URL进行OCR识别
- **智能参数**: 动态支持百度API所有参数和优先级逻辑
- **统一响应**: 标准化的JSON响应格式

### 🛡️ 安全与认证
- **API密钥认证**: 支持多种认证方式（Bearer Token、API Key等）
- **密码保护**: 管理面板密码保护和自动登录
- **环境变量**: 敏感信息通过环境变量安全管理

### 📱 现代化管理面板
- **实时监控**: 密钥健康状态、Token使用情况实时展示
- **可视化统计**: 月度使用量可视化图表和进度条
- **一键操作**: Token刷新、健康重置、数据清理等便捷操作
- **响应式设计**: 支持桌面和移动设备访问

## 🚀 快速开始

### 前置要求

- Python 3.8+
- Redis 服务器
- 百度智能云OCR API密钥

### 安装步骤

1. **克隆项目**
```bash
git clone https://github.com/yourusername/baidu-ocr-proxy.git
cd baidu-ocr-proxy
```

2. **安装依赖**
```bash
pip install -r requirements.txt
```

3. **配置环境变量**
```bash
cp .env.example .env
# 编辑 .env 文件，填入你的配置信息
```

4. **启动Redis服务**
```bash
# Windows
redis-server

# Linux/macOS
sudo systemctl start redis
```

5. **启动API服务**
```bash
python baidu_api.py
```

6. **启动管理面板**（可选）
```bash
python admin_baidu_gui.py
```

### 验证安装

访问以下地址验证服务是否正常运行：

- API服务: http://127.0.0.1:8080
- 管理面板: http://127.0.0.1:8181

## 🔧 环境配置

创建 `.env` 文件并配置以下参数：

```env
# 百度API密钥配置 (必需)
BAIDU_KEYS=[
  {
    "client_id": "your_client_id_1",
    "client_secret": "your_client_secret_1"
  },
  {
    "client_id": "your_client_id_2", 
    "client_secret": "your_client_secret_2"
  }
]

# Redis配置
REDIS_URL=redis://localhost:6379
REDIS_PASSWORD=

# API认证密钥 (必需)
API_KEY=your_secure_api_key

# 配额和限制设置
TOKEN_MAX_USES=30
MONTHLY_QUOTA_LIMIT=1000
QPS_LIMIT=2

# 健康检查配置
MAX_CONSECUTIVE_ERRORS=3
HEALTH_CHECK_INTERVAL=300

# 百度API地址 (通常无需修改)
BAIDU_TOKEN_URL=https://aip.baidubce.com/oauth/2.0/token
BAIDU_OCR_URL=https://aip.baidubce.com/rest/2.0/ocr/v1/multiple_invoice
```

## 📖 API文档

### 认证方式

所有API请求都需要包含认证信息，支持以下方式：

```bash
# 方式1: Authorization Header
curl -H "Authorization: Bearer YOUR_API_KEY" ...

# 方式2: X-API-Key Header  
curl -H "X-API-Key: YOUR_API_KEY" ...

# 方式3: API-Key Header
curl -H "API-Key: YOUR_API_KEY" ...
```

### 主要接口

#### 1. 文件上传OCR识别

```bash
POST /ocr/upload_smart
Content-Type: multipart/form-data

curl -X POST "http://127.0.0.1:8080/ocr/upload_smart" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "file=@invoice.jpg" \
  -F "probability=true" \
  -F "location=true"
```

#### 2. URL图片OCR识别

```bash
POST /ocr/url
Content-Type: application/json

curl -X POST "http://127.0.0.1:8080/ocr/url" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"url": "https://example.com/image.jpg"}'
```

#### 3. 获取Token信息

```bash
GET /token/info

curl -X GET "http://127.0.0.1:8080/token/info" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

#### 4. 刷新Token

```bash
POST /token/refresh

curl -X POST "http://127.0.0.1:8080/token/refresh" \
  -H "Authorization: Bearer YOUR_API_KEY"
```

### 响应格式

成功响应：
```json
{
  "words_result": [...],
  "words_result_num": 10,
  "log_id": 1234567890
}
```

错误响应：
```json
{
  "detail": "错误描述信息"
}
```

## 🎛️ 管理面板

管理面板提供直观的Web界面来监控和管理OCR服务：

### 功能特性

- **🔐 安全登录**: 密码保护，支持自动登录
- **📊 实时统计**: 密钥数量、健康状态、Token使用情况
- **🔑 Token管理**: 查看、刷新、清除Token
- **💚 健康监控**: 密钥健康状态监控和重置
- **📈 使用统计**: 月度使用量可视化展示
- **🧹 数据清理**: 一键清理孤立数据

### 访问方式

1. 启动管理面板服务
2. 访问 http://127.0.0.1:8181
3. 使用API_KEY作为密码登录

### 界面预览

管理面板采用现代化设计，支持：
- 响应式布局，适配各种设备
- 实时数据更新
- 直观的可视化图表
- 便捷的操作按钮

## ⚙️ 配置说明

### 密钥配置

支持多个百度API密钥配置，系统会自动进行负载均衡：

```json
{
  "client_id": "百度应用的API Key",
  "client_secret": "百度应用的Secret Key"
}
```

### 配额管理

- `TOKEN_MAX_USES`: 每个Token的最大使用次数
- `MONTHLY_QUOTA_LIMIT`: 每个密钥的月度配额限制
- `QPS_LIMIT`: 每秒查询次数限制

### 健康检查

- `MAX_CONSECUTIVE_ERRORS`: 连续错误次数阈值
- `HEALTH_CHECK_INTERVAL`: 健康检查间隔（秒）

## 🔍 故障排除

### 常见问题

#### 1. Redis连接失败
```
检查Redis服务是否启动
验证REDIS_URL和REDIS_PASSWORD配置
```

#### 2. 百度API调用失败
```
验证BAIDU_KEYS配置是否正确
检查网络连接是否正常
确认API密钥是否有效且有足够配额
```

#### 3. Token获取失败
```
检查client_id和client_secret是否正确
验证百度API服务是否正常
查看错误日志获取详细信息
```

## 🤝 贡献指南

欢迎提交Issue和Pull Request！

---

<div align="center">

**如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！**

Made with ❤️ by xlike

</div>