import json

import pytest

from core.api_clients.deepseek import CompletionResult
from core.config import DEFAULT_TRANSLATE_CONFIG
from core.tasks import translation_protocol as protocol
from core.tasks.translation_runtime import TranslationPaused, TranslationSession, translate_batch


def item(text, marker="Message"):
    return {"original_json_key": text, "text_to_translate": text,
            "original_marker": marker, "speaker_id": "NARRATION"}


def output(records, finish="stop"):
    return CompletionResult(content=json.dumps({"translations": records}, ensure_ascii=False), finish_reason=finish,
                            usage={"prompt_tokens": 50, "completion_tokens": 50, "total_tokens": 100})


class Client:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def complete(self, model, messages, **kwargs):
        self.calls.append((messages, kwargs))
        response = next(self.responses)
        return response(messages) if callable(response) else response


def config(**updates):
    return {**DEFAULT_TRANSLATE_CONFIG, "model": "fixture", "_apply_gbk_compatibility_postprocess": False, **updates}


def test_grouping_preserves_control_order_and_line_positions():
    encoded = protocol.encode("はい\\K\\N\\N\nいいえ\\C[1]")
    assert len(encoded.groups) == 2
    assert not encoded.restore(encoded.text.replace("[[C0]]", ""))[0]
    assert not encoded.restore(encoded.text.replace("[[C0]]", "[[C1]]"))[0]
    assert not encoded.restore(encoded.text.replace("[[C0]]\n", "\n[[C0]]"))[0]


def test_repeated_reasoning_only_truncation_stops_before_five_paid_attempts():
    bad = CompletionResult(content='', finish_reason='length', usage={
        'prompt_tokens':100, 'completion_tokens':8192, 'total_tokens':8292,
        'completion_tokens_details':{'reasoning_tokens':8192}})
    first = output([{'id':1,'text':'你好'}], finish='length')
    client = Client([first, bad, bad, bad, bad])
    saved = {}
    cfg = config(max_retries=3, retry_failed_items_only=True, _checkpoint_translation=lambda f,r:saved.update(r))
    session = TranslationSession(cfg)
    with pytest.raises(TranslationPaused, match='模型思考'):
        translate_batch([item('こんにちは'),item('記事です。')],[],[],[],client,{**cfg,'_translation_session':session})
    assert len(client.calls) == 3
    assert saved['こんにちは']['text'] == '你好'
    assert session.stats['reasoning_tokens'] == 16384


def test_actual_workflow_saves_partial_then_resumes_without_retranslation(monkeypatch, tmp_path):
    import queue
    from core.tasks import translate
    game = tmp_path / "game"
    works = tmp_path / "Works"
    folder = works / "game"
    (folder / "untranslated").mkdir(parents=True)
    data = {"Map1.txt": {"こんにちは": item("こんにちは"), "ありがとう": item("ありがとう")}}
    (folder / "untranslated/translation.json").write_text(json.dumps(data), encoding="utf-8")
    for filename, header in [("character_dictionary.csv", "原文,译文,对应原名,性别,年龄,性格,口吻,描述"), ("entity_dictionary.csv", "原文,译文,类别,描述")]:
        (folder / filename).write_text(header + "\n", encoding="utf-8")
    monkeypatch.setattr(translate.default_database, "load_default_db_mapping", lambda _: ({}, set()))
    client = Client([CompletionResult(content='{"translations":[{"id":1,"text":"你好"},', finish_reason="length"), CompletionResult(error_kind="quota", error="no credit")])
    monkeypatch.setattr(translate.deepseek, "DeepSeekClient", lambda *_: client)
    cfg = config(api_url="https://fixture.invalid/v1", api_key="fixture", concurrency=1, retry_failed_items_only=True)
    messages = queue.Queue()
    translate.run_translate(str(game), str(works), cfg, {}, messages)
    path = folder / "translated/translation_translated.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["Map1.txt"]["こんにちは"]["text"] == "你好"
    assert "ありがとう" not in saved["Map1.txt"]
    assert any(message[0] == "error" for message in list(messages.queue))
    client = Client([output([{"id":1,"text":"谢谢"}])])
    messages = queue.Queue()
    translate.run_translate(str(game), str(works), cfg, {}, messages)
    assert len(client.calls) == 1
    assert '"text": "こんにちは"' not in client.calls[0][0][1]["content"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert len(saved["Map1.txt"]) == 2
    client = Client([])
    translate.run_translate(str(game), str(works), cfg, {}, queue.Queue())
    assert not client.calls
