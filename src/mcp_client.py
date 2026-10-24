#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦当劳 MCP 客户端。

用标准 JSON-RPC 2.0 over HTTP 调用 https://mcp.mcd.cn，
把 35 个工具封装成 Python 方法，供「麦门省钱搭子」的上层逻辑使用。

设计要点
--------
1. **不硬编码任何门店/商品/价格**：所有事实数据都来自 MCP 实时返回。
2. **Token 不进仓库**：从环境变量 MCD_MCP_TOKEN 读取；
   本地开发时回退到 WorkBuddy 的连接器配置（~/.workbuddy/mcp.json）。
3. **优先 structuredContent**：MCP 返回的 `result.structuredContent` 是纯结构化数据，
   比 `result.content[].text` 里的 Markdown 可靠，因此优先使用，失败才回退解析文本。

单独运行可做连通性自检：
    python src/mcp_client.py --check
    python src/mcp_client.py --tools
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "https://mcp.mcd.cn"

# Windows 控制台默认 GBK，统一强制 UTF-8，避免中文与 ¥ 符号报 UnicodeEncodeError
for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream.encoding and _stream.encoding.lower().replace("-", "") != "utf8":
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TOKEN_ENV = "MCD_MCP_TOKEN"
URL_ENV = "MCD_MCP_URL"
# 用户级配置（面向"别人拿去用"的场景）
# 放在用户主目录而不是仓库里，避免 Token 混进 git，也避免和别的项目互相干扰。
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".maimen-budget-meal")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
# 仅本机开发用的兜底：从 WorkBuddy 连接器配置里读
WORKBUDDY_CFG = os.path.join(os.path.expanduser("~"), ".workbuddy", "mcp.json")

# beType: 1=到店自取 2=麦乐送 5=得来速 6=企业团餐
BE_TYPE = {"pickup": 1, "delivery": 2, "drivethru": 5, "catering": 6}
# orderType: 1=到店(含自取+得来速) 2=外送(含麦乐送+团餐)
ORDER_TYPE = {"pickup": 1, "delivery": 2, "drivethru": 1, "catering": 2}


class McdMCPError(RuntimeError):
    """MCP 调用失败（网络、鉴权、业务错误统一抛出）。"""

    def __init__(self, message: str, code: Any = None, payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


def _read_workbuddy_token() -> str:
    """本机开发兜底：从 WorkBuddy 连接器配置里取。别人的机器上通常没有这个文件。"""
    try:
        with open(WORKBUDDY_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        for _name, server in (cfg.get("mcpServers") or {}).items():
            headers = server.get("headers") or {}
            auth = headers.get("Authorization") or headers.get("authorization") or ""
            if isinstance(auth, str) and auth.lower().startswith("bearer "):
                return auth[7:].strip()
    except (OSError, json.JSONDecodeError):
        pass
    return ""


def load_token_with_source() -> tuple[str, str]:
    """按优先级取 Token，并返回它的**来源**，便于界面提示"你现在的凭证从哪来"。

    优先级：
      1. ``env``       环境变量 MCD_MCP_TOKEN —— 适合 CI / 高级用户
      2. ``file``      用户配置文件 ~/.maimen-budget-meal/config.json —— **普通用户推荐**
      3. ``workbuddy`` 本机 WorkBuddy 连接器配置 —— 仅本机开发兜底
      4. ``none``      没有配置
    """
    tok = (os.environ.get(TOKEN_ENV) or "").strip()
    if tok:
        return tok, "env"

    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
        tok = str(cfg.get("token") or "").strip()
        if tok:
            return tok, "file"
    except (OSError, json.JSONDecodeError):
        pass

    tok = _read_workbuddy_token()
    if tok:
        return tok, "workbuddy"

    return "", "none"


def load_token() -> str:
    """兼容旧调用：只要 Token，没有就抛带引导信息的错误。"""
    tok, _source = load_token_with_source()
    if tok:
        return tok
    raise McdMCPError("\n".join([
        "还没有配置麦当劳 MCP Token。",
        "  ① 打开 https://open.mcd.cn/mcp 用手机号登录 → 控制台 → 激活 → 申请 Token",
        "  ② 三种配置方式任选一种：",
        "     · 在网页界面点「配置 MCP Token」直接填入（推荐）",
        f"     · 设置环境变量 {TOKEN_ENV}",
        f'     · 写入 {CONFIG_FILE}，内容形如 {{"token": "你的Token"}}',
    ]))


def save_token(token: str) -> str:
    """把 Token 写入用户配置文件（权限尽量收紧），返回文件路径。"""
    token = (token or "").strip()
    if not token:
        raise McdMCPError("Token 不能为空")
    if len(token) < 12:
        raise McdMCPError("Token 长度看起来不对，请检查是否完整复制")

    os.makedirs(CONFIG_DIR, exist_ok=True)
    payload = {"token": token, "savedAt": __import__("datetime").datetime.now().isoformat(timespec="seconds")}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_FILE, 0o600)  # 仅本人可读
    except (OSError, NotImplementedError):
        pass
    return CONFIG_FILE


def clear_token() -> bool:
    """删除本机保存的 Token（环境变量不受影响）。"""
    try:
        os.remove(CONFIG_FILE)
        return True
    except OSError:
        return False


def mask_token(token: str) -> str:
    """脱敏展示，任何界面/日志都只能用这个。"""
    token = (token or "").strip()
    if not token:
        return ""
    if len(token) <= 8:
        return "*" * len(token)
    return f"{token[:4]}{'*' * 8}{token[-4:]}"


def _extract_json_from_text(text: str) -> Any:
    """从 MCP 的 text 块里抠出内嵌 JSON（兜底路径）。"""
    if not text:
        return None
    # 1) ```json ... ``` 代码块
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    # 2) 从最后一个 '{' 往回找能解析的最大 JSON
    for start in [i for i, ch in enumerate(text) if ch == "{"]:
        chunk = text[start:]
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    return None


class McdMCPClient:
    """麦当劳 MCP 的最小可用客户端。"""

    def __init__(self, url: str | None = None, token: str | None = None, timeout: int = 30):
        self.url = (url or os.environ.get(URL_ENV) or DEFAULT_URL).rstrip("/")
        self.token = token or load_token()
        self.timeout = timeout
        self._id = 0

    # ---------- 传输层 ----------

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params

        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"), method="POST"
        )
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise McdMCPError(f"MCP HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise McdMCPError(f"无法连接麦当劳 MCP（{self.url}）：{exc.reason}") from exc
        except TimeoutError as exc:
            raise McdMCPError(f"麦当劳 MCP 超时（>{self.timeout}s）") from exc

        # 兼容 SSE 响应
        stripped = raw.lstrip()
        if stripped.startswith("event:") or stripped.startswith("data:"):
            for line in raw.splitlines():
                if line.startswith("data:"):
                    raw = line[5:].strip()
                    break

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise McdMCPError(f"MCP 响应不是合法 JSON：{raw[:200]}") from exc

        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            raise McdMCPError(
                f"MCP 错误 {err.get('code')}: {err.get('message')}", code=err.get("code"), payload=err
            )
        return data.get("result") or {}

    # ---------- 协议层 ----------

    def initialize(self) -> dict:
        return self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcd-budget-meal", "version": "1.0.0"},
            },
        )

    def list_tools(self) -> list[dict]:
        return self._rpc("tools/list", {}).get("tools", [])

    def call(self, tool: str, arguments: dict | None = None) -> dict:
        """调用工具，返回业务数据 dict。

        成功时返回 MCP 的 structuredContent；若上游只给了 Markdown text，
        则回退解析内嵌 JSON。业务失败（success=false）抛 McdMCPError。
        """
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments or {}})

        if result.get("isError"):
            raise McdMCPError(f"工具 {tool} 返回错误", payload=result)

        data = result.get("structuredContent")
        if data is None:
            for block in result.get("content") or []:
                if block.get("type") == "text":
                    data = _extract_json_from_text(block.get("text", ""))
                    if data is not None:
                        break

        if data is None:
            raise McdMCPError(f"工具 {tool} 的返回无法解析为结构化数据", payload=result)

        # 上游统一用 success/code/message 表达业务结果
        if isinstance(data, dict) and data.get("success") is False:
            raise McdMCPError(
                f"{tool} 业务失败 {data.get('code')}: {data.get('message')}",
                code=data.get("code"),
                payload=data,
            )
        return data

    # ---------- 业务层封装 ----------

    def addresses(self) -> list[dict]:
        """外送可用地址列表。"""
        return (self.call("delivery-query-addresses").get("data") or {}).get("addresses") or []

    def nearby_stores(self, be_type: int = 1, search_type: int = 1,
                      city: str | None = None, keyword: str | None = None) -> list[dict]:
        """到店场景门店（beType=1 自提 / 5 得来速）。"""
        args: dict[str, Any] = {"beType": be_type, "searchType": search_type}
        if city:
            args["city"] = city
        if keyword:
            args["keyword"] = keyword
        data = self.call("query-nearby-stores", args)
        return data.get("data") or []

    def delivery_stores(self, address_id: str, be_type: int = 2) -> list[dict]:
        """外送场景可配送门店。"""
        data = self.call("delivery-query-stores", {"addressId": address_id, "beType": be_type})
        raw = data.get("data")
        if isinstance(raw, list):
            return raw
        if isinstance(raw, dict):
            for key in ("stores", "storeList", "list"):
                if isinstance(raw.get(key), list):
                    return raw[key]
        return []

    def meals(self, store_code: str, be_type: int = 1, be_code: str | None = None,
              order_type: int | None = None, reservation_date: str | None = None) -> Any:
        """门店餐品列表（含价格、可售状态）。

        参数传递规则（来自工具说明，务必遵守）：
        - beType=1 到店自取 -> orderType=1，**不传 beCode**
        - beType=5 得来速   -> orderType=1，必传 beCode
        - beType=2 麦乐送   -> orderType=2，必传 beCode
        - beType=6 团餐     -> orderType=2，必传 beCode
        """
        order_type = order_type if order_type is not None else ORDER_TYPE.get("pickup", 1)
        args: dict[str, Any] = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
        if be_code and be_type != 1:
            args["beCode"] = be_code
        if reservation_date:
            args["reservationDate"] = reservation_date
        return self.call("query-meals", args).get("data")

    def meal_detail(self, store_code: str, code: str, be_type: int = 1,
                    be_code: str | None = None, order_type: int | None = None) -> Any:
        """餐品详情（套餐组成、可特调项）。"""
        order_type = order_type if order_type is not None else ORDER_TYPE.get("pickup", 1)
        args: dict[str, Any] = {
            "storeCode": store_code, "orderType": order_type, "beType": be_type, "code": code,
        }
        if be_code and be_type != 1:
            args["beCode"] = be_code
        return self.call("query-meal-detail", args).get("data")

    def store_coupons(self, store_code: str, be_type: int = 1,
                      be_code: str | None = None, order_type: int | None = None) -> Any:
        """指定门店 + 订单类型下可用的优惠券。"""
        order_type = order_type if order_type is not None else ORDER_TYPE.get("pickup", 1)
        args: dict[str, Any] = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
        if be_code and be_type != 1:
            args["beCode"] = be_code
        return self.call("query-store-coupons", args).get("data")

    def available_coupons(self) -> Any:
        """当前可领取的优惠券。"""
        return self.call("available-coupons").get("data")

    def my_coupons(self, page: int = 1, page_size: int = 50) -> Any:
        """卡包内已有优惠券。"""
        return self.call("query-my-coupons", {"page": page, "pageSize": page_size}).get("data")

    def nutrition(self) -> Any:
        """常见餐品营养成分（用于热量/蛋白质约束）。"""
        return self.call("list-nutrition-foods").get("data")

    def calculate_price(self, store_code: str, items: list[dict], be_type: int = 1,
                        be_code: str | None = None, order_type: int | None = None,
                        reservation_date: str | None = None) -> dict:
        """按真实优惠规则算价。

        items: [{"productCode": "xxx", "quantity": 1, "couponId": "...", "couponCode": "..."}]
        """
        order_type = order_type if order_type is not None else ORDER_TYPE.get("pickup", 1)
        args: dict[str, Any] = {
            "storeCode": store_code, "orderType": order_type, "beType": be_type, "items": items,
        }
        if be_code and be_type != 1:
            args["beCode"] = be_code
        if reservation_date:
            args["reservationDate"] = reservation_date
        return self.call("calculate-price", args)

    def now(self) -> Any:
        """服务器时间（判断活动周期、券有效期时用）。"""
        return self.call("now-time-info").get("data")

    # ---------- 订单（本作品唯一会真实创建订单的能力，调用方必须先过演示模式/二次确认） ----------

    def create_order(self, store_code: str, items: list[dict], take_way_code: str,
                     be_type: int = 1, be_code: str | None = None, order_type: int = 1,
                     address_id: str | None = None, remark: str | None = None,
                     need_tableware: bool = False) -> dict:
        """创建真实订单，返回 data（含 orderId / payId / payH5Url / orderDetail）。

        参数来自 create-order 工具 schema（2026-10-24 实测）：
        - takeWayCode：orderType=1（到店/得来速）必传，**必须取自 calculate-price
          返回的 takeWayList[].code，严禁硬编码**；orderType=2 不传；
        - beCode：得来速(5)/外送(2)/团餐(6) 必传，到店自取(1) 不传；
        - items: [{"productCode": "xxx", "quantity": 1}]，与 calculate-price 的入参同构。
        """
        args: dict[str, Any] = {
            "storeCode": store_code, "items": items,
            "beType": be_type, "orderType": order_type,
        }
        if order_type == 1:
            if not take_way_code:
                raise McdMCPError("到店/得来速建单必须传 takeWayCode（来自 calculate-price 的 takeWayList）")
            args["takeWayCode"] = take_way_code
        if be_code and be_type != 1:
            args["beCode"] = be_code
        if address_id:
            args["addressId"] = address_id
        if remark:
            args["remark"] = remark[:50]
        if need_tableware:
            args["needTableware"] = True
        return (self.call("create-order", args).get("data") or {})

    def cancel_order(self, order_id: str, cancel_reason_code: str = "1") -> dict:
        """取消未支付订单，返回 data（含 cancelResult）。"""
        if not order_id:
            raise McdMCPError("取消订单必须传 orderId")
        return (self.call("cancel-order", {
            "orderId": str(order_id), "cancelReasonCode": str(cancel_reason_code),
        }).get("data") or {})

    def query_order(self, order_id: str) -> dict:
        """查询订单真实状态，返回 data（含 orderStatus / status）。

        支付结果只能靠它确认：status=10 待支付，60 已取消，其余以 orderStatus 文案为准。
        """
        if not order_id:
            raise McdMCPError("查询订单必须传 orderId")
        return (self.call("query-order", {"orderId": str(order_id)}).get("data") or {})


# ---------- 自检入口 ----------

def _main() -> int:
    ap = argparse.ArgumentParser(description="麦当劳 MCP 客户端自检")
    ap.add_argument("--check", action="store_true", help="握手 + 列工具数量")
    ap.add_argument("--tools", action="store_true", help="打印全部工具名与必填参数")
    ap.add_argument("--call", metavar="TOOL", help="调用指定工具")
    ap.add_argument("--args", default="{}", help="--call 的参数（JSON）")
    args = ap.parse_args()

    try:
        client = McdMCPClient()
    except McdMCPError as exc:
        print(f"[FATAL] {exc}", file=sys.stderr)
        return 2

    if args.check:
        info = client.initialize()
        tools = client.list_tools()
        print(f"serverInfo : {info.get('serverInfo')}")
        print(f"protocol   : {info.get('protocolVersion')}")
        print(f"tools      : {len(tools)}")
        return 0

    if args.tools:
        for t in client.list_tools():
            schema = t.get("inputSchema") or {}
            required = schema.get("required") or []
            print(f"{t['name']:<28} 必填={required}")
        return 0

    if args.call:
        print(json.dumps(client.call(args.call, json.loads(args.args)), ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
