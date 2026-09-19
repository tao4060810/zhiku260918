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

    def test_only_selected_images_from_cited_local_docs_with_limit(self):
        # 混合重复图片、外部地址和网络资料图片，验证本地来源白名单及三张上限。
        urls = [f"/assets/{uuid4()}" for i in range(5)]
        docs = [{"source": "local", "kb_id":self.kb_id,"document_id":str(uuid4()), "content": "\n".join(f"![diagram]({u})" for u in urls)},
                {"source": "web", "content": "![web](https://example.com/web.png)"}]
        text = "Steps[cite:1][cite:2]\n【图片】\n" + "\n".join([urls[3], urls[3], "https://evil.test/x.png", "https://example.com/web.png", *urls])
        result = present_answer(text, docs)
        self.assertEqual(result["answer"], "Steps[cite:1][cite:2]")
        self.assertEqual(result["image_urls"], [urls[3], urls[0], urls[1]])
        self.assertEqual(present_answer("Steps[cite:1]", docs)["image_urls"], [])
        self.assertEqual(present_answer(text, [])['image_urls'], [])

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
