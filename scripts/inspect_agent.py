"""本地检查图结构/工具契约；不实例化模型、不查询或修改数据库。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent import default_registry  # noqa: E402
from app.agent.graph import InvestigationGraph  # noqa: E402


def inspect_agent(format: str = "json") -> str:
    graph = InvestigationGraph()
    if format == "mermaid":
        return graph.describe()
    return json.dumps({"graph": graph.compiled.get_graph().to_json(),
                       "state_schema": graph.compiled.get_input_jsonschema(),
                       "tools": default_registry().describe()}, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=("json", "mermaid"), default="json")
    args = parser.parse_args()
    print(inspect_agent(args.format))


if __name__ == "__main__":
    main()
