"""Retain typed body-less 304 responses omitted by the TS generator."""

import json
import re
import sys
from pathlib import Path

ALIASES = {
    "get_game_api_v1_catalog_games__game_id__get": (
        "getGameApiV1CatalogGamesGameIdGetResponses",
    ),
    "get_box_art_api_v1_catalog_games__game_id__box_art_get": (
        "getBoxArtApiV1CatalogGamesGameIdBoxArtGetResponses",
    ),
}

openapi_path, types_path = map(Path, sys.argv[1:3])
schema = json.loads(openapi_path.read_text())
types = types_path.read_text()
for path_item in schema["paths"].values():
    for operation in path_item.values():
        if not isinstance(operation, dict) or "304" not in operation.get("responses", {}):
            continue
        alias = ALIASES.get(operation.get("operationId"))
        if alias is None:
            continue
        for response_alias in alias:
            pattern = re.compile(
                rf"(export type {re.escape(response_alias)} = \{{)(.*?)(\n\}};)",
                re.DOTALL,
            )
            match = pattern.search(types)
            if match is None:
                raise SystemExit(f"generated response type not found: {response_alias}")
            if re.search(r"^\s*304:", match.group(2), re.MULTILINE):
                continue
            description = operation["responses"]["304"].get("description", "Not modified")
            response_block = (
                f"{match.group(2)}\n"
                f"    /**\n     * {description}\n     */\n"
                "    304: undefined;"
            )
            types = types[: match.start(2)] + response_block + types[match.end(2) :]

types_path.write_text(types)
