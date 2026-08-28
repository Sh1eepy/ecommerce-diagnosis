"""本机 stdio MCP 入口。由经授权的客户端启动；stdout 仅供协议使用。"""
from __future__ import annotations

import asyncio
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


async def serve(server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="本机 stdio MCP 只读工具服务；默认关闭，无 HTTP 端口。")
    parser.parse_args(argv)  # --help 不加载应用配置、不访问数据库、不启动服务。
    # 不调用 init_db，不启动 Worker，不读取/生成诊断，不输出配置或异常明文。
    try:
        from app.mcp_server import create_server

        server = create_server()
    except Exception:
        print("MCP 启动被拒：请核对依赖、配置、显式启用和 tools:read 授权。", file=sys.stderr)
        return 2
    try:
        asyncio.run(serve(server))
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("MCP 运行失败；请检查受限日志，不要把凭证或异常明文发给客户端。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
