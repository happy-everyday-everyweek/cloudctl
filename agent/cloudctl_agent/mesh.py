"""局域网互联：邻居发现 + 点对点命令转发 + 文件互传。

设计约束：
1. 只用标准库（socket / http.server / urllib / threading），不引入任何 UI 或
g   图形依赖，不弹窗、不托盘、不提示，设备端只当被控端。
2. 发现用 UDP（组播 239.255.42.99 收，组播与广播双发），对端不可达不影响本机。
3. 通信走局域网 HTTP，每个请求带共享令牌；命令进入同一个 Router 权限门禁。
4. 允许一跳以上中继：ttl 递减，超过 mesh_max_hops 丢弃，避免广播风暴。

协议版本 v1，信封字段：v、kind、mesh、from、from_name、to、id、ts、ttl、hops。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import socket
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import ops

The user asked for this module.Now write it. But I must output only the JSON param... (I need to write the real file content here - no meta text).
