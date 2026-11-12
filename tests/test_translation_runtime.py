import json

import pytest

from core.api_clients.deepseek import CompletionResult
from core.config import DEFAULT_TRANSLATE_CONFIG
from core.tasks import translation_protocol as protocol
from core.tasks.translation_runtime import TranslationPaused, TranslationSession, translate_batch, validate_item


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


@pytest.mark.parametrize("Source_Text", [
    "　\\>名前\\<  \n\n  \\!\n　本文\\.続き \n",
    "\\>\\<", "\n", "本文[[C0]]\\.", "  名前\r\n　本文  ",
])
def test_Transport_SegmentsRoundTripProgramOwnedStructure(Source_Text):
    Task_Transport = protocol.encode(Source_Text)
    Request_Segments = Task_Transport.Transport_BuildModelSegments()
    assert Task_Transport.Transport_RestoreModelSegments(Request_Segments) == (True, Source_Text, "")
    assert all(not list(protocol.control_tokens.iter_token_occurrences(
        Segment_Row["text"], include_quotes=False)) for Segment_Row in Request_Segments)


def test_Transport_SegmentsRestoreInteriorControlsAndProtectedLiterals():
    Source_Text = "　\\>名前\\<\n　本文\\.続き URL https://example.test \n"
    Task_Transport = protocol.encode(Source_Text, extra_literals=("https://example.test",))
    Request_Segments = Task_Transport.Transport_BuildModelSegments()
    assert Request_Segments == [
        {"id": 1, "text": "名前"}, {"id": 2, "text": "本文"},
        {"id": 3, "text": "続き URL"},
    ]
    Response_Segments = [{"id": 3, "text": "后文 URL"}, {"id": 1, "text": "姓名"}, {"id": 2, "text": "正文"}]
    Task_Result, Task_Reason = validate_item(item(Source_Text), Task_Transport, {"segments": Response_Segments}, config())
    assert Task_Result is not None, Task_Reason
    assert Task_Result["text"] == "　\\>姓名\\<\n　正文\\.后文 URL https://example.test \n"


@pytest.mark.parametrize("Response_Segments", [
    [], [{"id": 1, "text": ""}], [{"id": True, "text": "正文"}],
    [{"id": 2, "text": "正文"}], [{"id": 1, "text": "正文\n额外行"}],
    [{"id": 1, "text": "正文\\!"}], [{"id": 1, "text": "正文", "extra": 1}],
    [{"id": 1, "text": "正文"}, {"id": 1, "text": "正文"}],
])
def test_Transport_RejectsInvalidSegments(Response_Segments):
    assert not protocol.encode("本文").Transport_RestoreModelSegments(Response_Segments)[0]


def test_Transport_ParsesJsonWithoutDependingOnFieldOrder():
    Response_Text = '{"type":"json_object","translations":[{"id":1,"segments":[{"id":1,"text":"你好"}]}]}'
    assert protocol.parse_records(Response_Text, {1}) == ({1: {"segments": [{"id": 1, "text": "你好"}]}}, "")
    assert not protocol.parse_records('{"translations":[],"translations":[]}', {1})[0]


def test_Transport_RecoversCompleteSegmentRecordBeforeTruncation():
    Response_Text = '{"translations":[{"id":1,"segments":[{"id":1,"text":"你好"}]},{"id":2,"segments":['
    Response_Records, Response_Error = protocol.parse_records(Response_Text, {1, 2})
    assert Response_Records == {1: {"segments": [{"id": 1, "text": "你好"}]}}
    assert Response_Error


def test_Translation_UsesSegmentsWithoutSendingControlMarkers():
    Source_Text = "\\>【恍惚なる闇】\\<\n「ナイは、\\.強力な洗脳魔法を\n　使います。\\!\n　それを考えると・・・」"
    Response_Texts = ["【恍惚之暗】", "「奈伊", "会使用强力的", "洗脑魔法。", "考虑到这一点……」"]
    Task_Client = Client([output([{
        "id": 1,
        "segments": [{"id": Segment_Id, "text": Segment_Text}
                     for Segment_Id, Segment_Text in enumerate(Response_Texts, 1)],
    }])])
    Task_Result = translate_batch([item(Source_Text)], [], [], [], Task_Client, config())
    assert Task_Result[Source_Text]["text"] == "\\>【恍惚之暗】\\<\n「奈伊\\.会使用强力的\n　洗脑魔法。\\!\n　考虑到这一点……」"
    Request_Prompt = Task_Client.calls[0][0][1]["content"]
    assert '"segments":' in Request_Prompt
    assert "[[C" not in Request_Prompt
    assert "\\." not in Request_Prompt
    assert "\\!" not in Request_Prompt


def test_Translation_RetriesOnlyFailedEntryWithOriginalBatchContext():
    Source_First = "こんにちは\\!"
    Source_Second = "ありがとう\\."
    Task_Saved = {}

    def Request_ReturnRetryResult(Request_Messages):
        assert Task_Saved[Source_First]["text"] == "你好\\!"
        assert Source_Second not in Task_Saved
        return output([{"id": 1, "segments": [{"id": 1, "text": "谢谢"}]}])

    Task_Client = Client([
        output([{"id": 1, "segments": [{"id": 1, "text": "你好"}]}, {"id": 2, "segments": []}]),
        Request_ReturnRetryResult,
    ])
    Task_Result = translate_batch([item(Source_First), item(Source_Second)], [], [], [], Task_Client,
                                  config(_checkpoint_translation=lambda Task_File, Task_Results: Task_Saved.update(Task_Results)))
    assert len(Task_Client.calls) == 2
    assert Task_Saved == Task_Result
    assert Task_Result[Source_First]["text"] == "你好\\!"
    assert Task_Result[Source_Second]["text"] == "谢谢\\."
    Retry_Prompt = Task_Client.calls[1][0][1]["content"]
    Retry_Target = Retry_Prompt.split("待翻译条目：\n", 1)[1].split("\n待修正项", 1)[0]
    assert len(json.loads(Retry_Target)) == 1
    assert "こんにちは" not in Retry_Target
    assert "こんにちは" in Retry_Prompt
    assert "缺少正文片段编号" in Retry_Prompt
    assert "previous_translation" in Retry_Prompt
    assert "[[C" not in Retry_Prompt
    assert "\\!" not in Retry_Prompt


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
