# 答案展示回归测试：覆盖旧版引用兼容、来源校验和图片筛选。
import copy
from uuid import uuid4
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils.answer_presentation import history_text, present_answer, present_history_message


class AnswerPresentationTests(unittest.TestCase):
    def setUp(self):
        self.user_id=str(uuid4());self.kb_id=str(uuid4())
        self.enterContext(patch("processor.query_processor.nodes.node_answer_output.require_kb_permission"))
        self.docs = [
            {"title": "First", "source": "local", "kb_id":self.kb_id,"document_id":str(uuid4()), "content": "First evidence"},
            {"title": "Second", "source": "local", "kb_id":self.kb_id,"document_id":str(uuid4()), "content": "Second evidence"},
        ]

    def test_legacy_history_resolves_original_ids_without_mutation(self):
        # 展示顺序可以变化，但编号仍应指向原资料，且不能修改传入的历史记录。
        record = {"role": "assistant", "text": "根据参考内容，步骤。（参考内容[2]）另一步。（参考内容[2][1]）", "sources": self.docs}
        original = copy.deepcopy(record)
        result = present_history_message(record)
        self.assertEqual(result["text"], "步骤。[cite:2]另一步。[cite:1]")
        self.assertEqual([s["title"] for s in result["sources"]], ["Second", "First"])
        self.assertEqual(record, original)
        self.assertEqual(present_history_message(result), result)

    def test_invalid_citations_and_uncited_sources_are_not_presented(self):
        result = present_answer("Fact.[cite:2] Unknown.[cite:99]", self.docs)
        self.assertEqual(result["answer"], "Fact.[cite:2] Unknown.")
        self.assertEqual([s["source_id"] for s in result["sources"]], ["2"])
        self.assertEqual(present_answer("Hello", self.docs)["sources"], [])

    def test_short_legacy_citation_forms(self):
        for label in ("参考", "参考内容", "参考资料"):
            self.assertEqual(present_answer(f"Fact（{label}[2][1]）", self.docs)["answer"], "Fact[cite:2][cite:1]")

    def test_code_and_user_messages_are_not_rewritten(self):
        text = "`[cite:99]`\n\n```text\n（参考内容[2]）\n【图片】\n```"
        self.assertEqual(present_answer(text, self.docs)["answer"], text)
        user = {"role": "user", "text": "根据参考内容，解释 [cite:1]"}
        self.assertEqual(present_history_message(user), user)

    def test_only_selected_images_from_local_docs_without_truncating(self):
        # 混合重复图片、外部地址和网络资料图片，验证白名单、去重及多图完整保留。
        urls = [f"/assets/{uuid4()}" for i in range(5)]
        docs = [{"source": "local", "kb_id":self.kb_id,"document_id":str(uuid4()), "content": "\n".join(f"![diagram]({u})" for u in urls)},
                {"source": "web", "content": "![web](https://example.com/web.png)"}]
        text = "Steps[cite:1][cite:2]\n【图片】\n" + "\n".join([urls[3], urls[3], "https://evil.test/x.png", "https://example.com/web.png", *urls])
        result = present_answer(text, docs)
        self.assertEqual(result["answer"], "Steps[cite:1][cite:2]")
        self.assertEqual(result["image_urls"], [urls[3], urls[0], urls[1], urls[2], urls[4]])
        self.assertEqual(present_answer("Steps[cite:1]", docs)["image_urls"], [])
        self.assertEqual(present_answer(text, [])['image_urls'], [])

    def test_uncited_image_source_is_saved_and_survives_history(self):
        # 正文引用步骤，配图来自另一个片段；回放时保留配图来源但不增加正文引用。
        url = f"/assets/{uuid4()}"
        docs = [self.docs[0], {**self.docs[1], "content": f"![连接示意图]({url})"}]
        result = present_answer("Steps[cite:1]", docs, [url])
        self.assertEqual(result["answer"], "Steps[cite:1]")
        self.assertEqual(result["image_urls"], [url])
        self.assertEqual([s["source_id"] for s in result["sources"]], ["1", "2"])
        record = {"role": "assistant", "text": result["answer"],
                  "sources": result["sources"], "image_urls": result["image_urls"]}
        self.assertEqual(present_history_message(record), record)
        self.assertEqual(present_answer("Diagram", docs, [url])["image_urls"], [url])

    def test_explicit_selection_overrides_inline_images_and_rejects_unknown_assets(self):
        url, foreign = f"/assets/{uuid4()}", f"/assets/{uuid4()}"
        docs = [{**self.docs[0], "content": f"![diagram]({url})"}]
        text = f"Steps[cite:1]\n【图片】\n{url}"
        self.assertEqual(present_answer(text, docs, [foreign])["image_urls"], [])
        self.assertEqual(present_answer(text, docs, [])["image_urls"], [])

    def test_inline_images_keep_position_and_history_without_extra_selection(self):
        urls = [f"/assets/{uuid4()}" for _ in range(4)]
        docs = [{**self.docs[0], "content": "\n".join(f"![diagram]({url})" for url in urls)}]
        text = "\n\n".join(f"Step {i}[cite:1]\n\n![diagram]({url})" for i, url in enumerate(urls)) + "\n\nDone."
        result = present_answer(text, docs)
        self.assertEqual(result["answer"], text)
        self.assertEqual(result["image_urls"], urls)
        record = {"role": "assistant", "text": text, "sources": result["sources"], "image_urls": urls}
        self.assertEqual(present_history_message(record), record)

    def test_invalid_and_duplicate_inline_images_are_removed(self):
        own, foreign = f"/assets/{uuid4()}", f"/assets/{uuid4()}"
        docs = [{**self.docs[0], "content": f"![diagram]({own})"}]
        text = (f"Before\n\n![diagram]({own})\n\nAfter\n\n![repeat]({own})"
                f"\n![foreign]({foreign})\n![remote](https://evil.test/x.png)\n<img src='{foreign}'>")
        result = present_answer(text, docs)
        self.assertEqual(result["answer"], f"Before\n\n![diagram]({own})\n\nAfter")
        self.assertEqual(result["image_urls"], [own])
        self.assertEqual(len(result["sources"]), 1)

    def test_history_prompt_removes_inline_assets_but_keeps_code_examples(self):
        url = f"/assets/{uuid4()}"
        text = f"Before\n![diagram]({url})\nAfter[cite:1] {url}"
        self.assertEqual(history_text(text), "Before\n\nAfter")
        code = f"`![example]({url})`"
        self.assertEqual(present_answer(code, self.docs)["answer"], code)
        self.assertEqual(present_answer(code, self.docs)["image_urls"], [])

    def test_answer_generation_uses_one_model_call_and_original_question(self):
        from processor.query_processor.nodes.node_answer_output import NodeAnswerOutput
        state = {"user_id": self.user_id, "kb_id": self.kb_id, "original_query": "请图文交替说明",
                 "rewritten_query": "配置方法",
                 "reranked_docs": self.docs, "is_stream": False}
        llm = SimpleNamespace(invoke=lambda prompt: SimpleNamespace(content="Steps[cite:1]"))
        with patch("processor.query_processor.nodes.node_answer_output.get_llm_client", return_value=llm) as client, \
             patch("processor.query_processor.nodes.node_answer_output.save_chat_message"):
            result = NodeAnswerOutput().process(state)
        self.assertEqual(result["answer"], "Steps[cite:1]")
        self.assertEqual(result["image_urls"], [])
        self.assertIn("请图文交替说明", result["prompt"])
        client.assert_called_once_with()

    def test_legacy_does_not_show_all_retrieved_images(self):
        record = {"role": "assistant", "text": "Answer（参考内容[1]）", "sources": self.docs, "image_urls": ["https://example.com/unused.png"]}
        self.assertEqual(present_history_message(record)["image_urls"], [])

    def test_history_prompt_does_not_reuse_previous_citation_ids(self):
        self.assertEqual(history_text("根据参考内容，步骤。（参考内容[4]）\n【图片】\nhttps://example.com/x.png"), "步骤。")

    def test_budget_excluded_documents_cannot_be_cited(self):
        # 第二份资料超过上下文预算；未提供给模型的内容不能成为有效引用。
        from processor.query_processor.nodes.node_answer_output import NodeAnswerOutput
        state = {"user_id":self.user_id,"kb_id":self.kb_id,"original_query": "How?", "reranked_docs": [self.docs[0], {**self.docs[1], "content": "x" * 13000}]}
        node = NodeAnswerOutput()
        node._step_2_construct_prompt(state)
        self.assertEqual([s["source_id"] for s in state["sources"]], ["1"])
        self.assertEqual(present_answer("Fact[cite:2]", state["sources"])["sources"], [])

    def test_streaming_and_blocking_store_the_same_validated_answer(self):
        # 用替身隔离模型、数据库和事件推送，验证两种生成模式保存相同的校验结果。
        from processor.query_processor.nodes.node_answer_output import NodeAnswerOutput
        for streaming in (True, False):
            with self.subTest(streaming=streaming):
                llm = SimpleNamespace(stream=lambda prompt: iter([SimpleNamespace(content="Answer[cite:"), SimpleNamespace(content="2][cite:99]")]), invoke=lambda prompt: SimpleNamespace(content="Answer[cite:2][cite:99]"))
                state = {"user_id":self.user_id,"kb_id":self.kb_id,"original_query": "How?", "reranked_docs": self.docs, "is_stream": streaming}
                with patch("processor.query_processor.nodes.node_answer_output.get_llm_client", return_value=llm), patch("processor.query_processor.nodes.node_answer_output.save_chat_message") as save, patch("processor.query_processor.nodes.node_answer_output.push_to_session"), patch("processor.query_processor.nodes.node_answer_output.set_task_result"):
                    result = NodeAnswerOutput().process(state)
                self.assertEqual(result["answer"], "Answer[cite:2]")
                self.assertEqual(save.call_args.kwargs["text"], result["answer"])
                self.assertEqual([s["title"] for s in save.call_args.kwargs["sources"]], ["Second"])


if __name__ == "__main__":
    unittest.main()
