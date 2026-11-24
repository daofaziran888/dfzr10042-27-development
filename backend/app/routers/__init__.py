"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import comparison_scope, resource

# comparison_scope 的固定前缀必须在 /api/{module} 之前注册，
# 否则 "comparison-scope" 会被当成模块 key。
ROUTERS = [comparison_scope, resource]
