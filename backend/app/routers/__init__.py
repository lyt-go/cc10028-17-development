"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, term_dict

# 术语字典等专用路由含 /api/{module}/... 的固定子路径，必须排在通用资源
# 路由（/api/{module} 动态路径）之前注册。
ROUTERS = [term_dict, resource]
