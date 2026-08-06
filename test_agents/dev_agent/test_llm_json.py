"""
test_agents/dev_agent/test_llm_json.py -- regression tests for
agents/dev_agent/llm_json.py's lenient LLM-response JSON parsing.
"""
import pytest

from agents.dev_agent import llm_json


class TestParseObject:
    def test_parses_raw_json(self):
        assert llm_json.parse_object('{"a": 1, "b": "two"}') == {"a": 1, "b": "two"}

    def test_parses_fenced_json_block(self):
        text = 'Here is the result:\n```json\n{"a": 1}\n```\nLet me know if you need more.'
        assert llm_json.parse_object(text) == {"a": 1}

    def test_parses_fence_without_json_language_tag(self):
        text = '```\n{"a": 1}\n```'
        assert llm_json.parse_object(text) == {"a": 1}

    def test_parses_json_embedded_in_prose_without_fence(self):
        text = 'Sure, here you go: {"a": 1, "b": [1, 2, 3]} -- hope that helps!'
        assert llm_json.parse_object(text) == {"a": 1, "b": [1, 2, 3]}

    def test_raises_on_pure_prose(self):
        with pytest.raises(llm_json.LLMResponseParseError):
            llm_json.parse_object("I could not find any issues in this code.")

    def test_raises_on_json_array_not_object(self):
        with pytest.raises(llm_json.LLMResponseParseError):
            llm_json.parse_object("[1, 2, 3]")

    def test_raises_on_empty_string(self):
        with pytest.raises(llm_json.LLMResponseParseError):
            llm_json.parse_object("")

    def test_raises_on_none(self):
        with pytest.raises(llm_json.LLMResponseParseError):
            llm_json.parse_object(None)
