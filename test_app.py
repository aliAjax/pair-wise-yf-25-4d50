import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import BusinessError, ReviewStore


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReviewStore(Path(self.tmp.name) / "test.db")
        self.store.seed()

    def tearDown(self):
        self.tmp.cleanup()

    def _paper(self):
        return self.store.submit_paper("alice", "可靠分布式提交协议", "本文提出一种用于弱网环境的可靠提交协议，并通过模拟实验验证其安全性和性能。")["id"]

    def _two_reviews(self, paper_id):
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        return a1, a2

    def _reviewer_load(self, reviewer_id):
        with self.store.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM assignments WHERE reviewer_id=? AND status IN ('invited','accepted')",
                (reviewer_id,),
            ).fetchone()[0]

    def test_complete_flow_and_double_blind_view(self):
        paper_id = self._paper()
        a1 = self.store.assign("chair", paper_id, "r1")["id"]
        a2 = self.store.assign("chair", paper_id, "r2")["id"]
        self.store.respond_assignment("r1", a1, True)
        self.store.respond_assignment("r2", a2, True)
        self.store.submit_review("r1", a1, 4, "方法严谨，缺少与最近工作的对比。")
        self.store.submit_review("r2", a2, 3, "实验充分，但部分结论需要进一步解释。")
        self.store.submit_rebuttal("alice", paper_id, "感谢意见，我们将补充对比并解释实验结论。")
        result = self.store.decide("chair", paper_id, "minor_revision", "补充实验后接收。")
        self.assertEqual(result["decision"], "minor_revision")
        self.assertIsNone(self.store.get_paper("r1", paper_id)["author_id"])
        self.assertIsNotNone(self.store.get_paper("chair", paper_id)["author_id"])
        history = self.store.history("chair", paper_id)
        self.assertEqual(history[-1]["action"], "decision.record")
        self.assertGreaterEqual(len(history), 8)

    def test_conflict_blocks_assignment_and_role_is_enforced(self):
        paper_id = self._paper()
        self.store.add_conflict("chair", paper_id, "r1", "同一导师团队成员")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("chair", paper_id, "r1")
        self.assertEqual(ctx.exception.code, "conflict_of_interest")
        with self.assertRaises(BusinessError) as ctx:
            self.store.assign("alice", paper_id, "r2")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_paper("r2", paper_id)
        self.assertEqual(ctx.exception.status, 403)

    def test_withdraw_cancels_pending_and_releases_load_but_keeps_completed(self):
        paper_id = self._paper()
        a1, a2 = self._two_reviews(paper_id)
        a3 = self.store.assign("chair", paper_id, "r3")["id"]  # r3 仍处于 invited。
        self.assertEqual(self._reviewer_load("r3"), 1)
        result = self.store.withdraw_paper("alice", paper_id, "发现实验数据有误，需要重做后再投稿。")
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["canceled_assignments"], [a3])
        self.assertEqual(self.store.get_paper("alice", paper_id)["status"], "withdrawn")
        with self.store.connect() as conn:
            states = {
                row["id"]: row["status"]
                for row in conn.execute("SELECT id,status FROM assignments WHERE paper_id=?", (paper_id,))
            }
        # 未处理邀请取消，已完成意见保留。
        self.assertEqual(states[a1], "completed")
        self.assertEqual(states[a2], "completed")
        self.assertEqual(states[a3], "canceled")
        self.assertEqual(self._reviewer_load("r3"), 0)
        # 取消后的邀请不能再接受，已撤回论文也不能再投标。
        with self.assertRaises(BusinessError) as ctx:
            self.store.respond_assignment("r3", a3, True)
        self.assertEqual(ctx.exception.status, 409)
        with self.assertRaises(BusinessError) as ctx:
            self.store.bid("r3", paper_id, "want")
        self.assertEqual(ctx.exception.status, 409)
        # 时间线保留给主席：撤稿与取消动作均有记录。
        actions = [entry["action"] for entry in self.store.history("chair", paper_id)]
        self.assertIn("paper.withdraw", actions)
        self.assertIn("assignment.cancel", actions)
        self.assertEqual(len(self.store.history("r3", paper_id)), len(actions))
        # 已完成评审内容没有丢失。
        with self.store.connect() as conn:
            kept = conn.execute("SELECT score,review_text FROM assignments WHERE id=?", (a1,)).fetchone()
        self.assertEqual(kept["score"], 4)

    def test_withdraw_validations_and_double_withdraw_conflict(self):
        paper_id = self._paper()
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_paper("alice", paper_id, "太短")
        self.assertEqual(ctx.exception.code, "invalid_reason")
        with self.assertRaises(BusinessError) as ctx:  # 主席不能替作者撤稿。
            self.store.withdraw_paper("chair", paper_id, "主席测试撤稿原因。")
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(BusinessError) as ctx:  # 只能撤自己的论文。
            self.store.withdraw_paper("bob", paper_id, "bob 尝试撤回 alice 的论文。")
        self.assertEqual(ctx.exception.status, 403)
        self.store.withdraw_paper("alice", paper_id, "作者主动撤回，待修改后重新投稿。")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_paper("alice", paper_id, "再次尝试撤回已撤回的论文。")
        self.assertEqual(ctx.exception.code, "already_withdrawn")
        # 已决定的论文不能撤回。
        decided = self._paper()
        self._two_reviews(decided)
        self.store.decide("chair", decided, "accept")
        with self.assertRaises(BusinessError) as ctx:
            self.store.withdraw_paper("alice", decided, "决定后才想起来要撤回。")
        self.assertEqual(ctx.exception.code, "paper_decided")

    def test_concurrent_withdraw_and_decision_only_one_succeeds(self):
        paper_id = self._paper()
        self._two_reviews(paper_id)
        outcomes = {}

        def withdraw():
            try:
                self.store.withdraw_paper("alice", paper_id, "并发场景下作者决定撤回论文。")
                outcomes["withdraw"] = None
            except BusinessError as exc:
                outcomes["withdraw"] = exc.status

        def decide():
            try:
                self.store.decide("chair", paper_id, "accept", "并发决定。")
                outcomes["decision"] = None
            except BusinessError as exc:
                outcomes["decision"] = exc.status

        barrier = threading.Barrier(2)

        def run(fn):
            barrier.wait()  # 尽量让两个事务在同一时刻抢锁。
            fn()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(run, (withdraw, decide)))
        self.assertEqual(set(outcomes), {"withdraw", "decision"})
        successes = [name for name, code in outcomes.items() if code is None]
        self.assertEqual(len(successes), 1, outcomes)
        self.assertEqual([code for code in outcomes.values() if code is not None], [409], outcomes)
        final_status = self.store.get_paper("chair", paper_id)["status"]
        self.assertIn(final_status, {"decided", "withdrawn"})
        # 再来一次：失败方重试仍然只能拿到 409，状态保持不变。
        if final_status == "withdrawn":
            with self.assertRaises(BusinessError) as ctx:
                self.store.decide("chair", paper_id, "accept")
            self.assertEqual(ctx.exception.status, 409)
        else:
            with self.assertRaises(BusinessError) as ctx:
                self.store.withdraw_paper("alice", paper_id, "决定后再尝试撤稿。")
            self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(self.store.get_paper("chair", paper_id)["status"], final_status)

    def test_chair_can_filter_withdrawn_papers(self):
        withdrawn = self._paper()
        active = self.store.submit_paper("bob", "另一个活跃投稿", "这是一篇仍在评审流程中的论文，摘要长度满足校验要求。")["id"]
        self.store.withdraw_paper("alice", withdrawn, "作者撤稿用于筛选测试。")
        all_papers = {p["id"]: p["status"] for p in self.store.list_papers("chair")}
        self.assertEqual(all_papers[withdrawn], "withdrawn")
        self.assertIn(active, all_papers)
        withdrawn_list = self.store.list_papers("chair", status="withdrawn")
        self.assertEqual([p["id"] for p in withdrawn_list], [withdrawn])
        active_list = {p["id"] for p in self.store.list_papers("chair", status="under_review")}
        self.assertNotIn(withdrawn, active_list)
        with self.assertRaises(BusinessError) as ctx:
            self.store.list_papers("chair", status="bogus")
        self.assertEqual(ctx.exception.code, "invalid_status")


if __name__ == "__main__":
    unittest.main()
