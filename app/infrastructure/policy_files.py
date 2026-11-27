import json
from pathlib import Path

from yaml import SafeLoader, YAMLError
from yaml.events import AliasEvent
from yaml.nodes import MappingNode, Node

from app.domain.qa import InvalidPolicyError
from app.domain.qa_sampling import QaSamplingPolicy


def unique_policy_fields(pairs: list[tuple[object, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if not isinstance(key, str) or key in result:
            raise ValueError("Policy fields must have unique string names")
        result[key] = value
    return result


class _PolicyYamlLoader(SafeLoader):
    def compose_node(self, parent: Node | None, index: int | Node | None) -> Node:
        if self.check_event(AliasEvent):
            raise YAMLError("Policy YAML must define each value directly")
        return super().compose_node(parent, index)

    def construct_mapping(
        self, node: MappingNode, deep: bool = False
    ) -> dict[str, object]:
        return unique_policy_fields(self.construct_pairs(node, deep=deep))


def _yaml_document(text: str) -> object:
    loader = _PolicyYamlLoader(text)
    try:
        return loader.get_single_data()
    finally:
        loader.dispose()


def read_policy_data(path: Path) -> dict[str, object]:
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix.lower() in {".yaml", ".yml"}:
            document = _yaml_document(text)
        else:
            document = json.loads(text, object_pairs_hook=unique_policy_fields)
        if not isinstance(document, dict):
            raise ValueError("Policy document must be a mapping")
        return document
    except (ValueError, RecursionError, YAMLError) as error:
        raise InvalidPolicyError(f"Invalid policy file: {path.name}") from error


def load_qa_sampling_policy(path: Path) -> QaSamplingPolicy:
    document = read_policy_data(path)
    if not isinstance(document.get("version"), str):
        raise InvalidPolicyError(f"Invalid sampling policy: {path.name}")
    return QaSamplingPolicy(**document)
