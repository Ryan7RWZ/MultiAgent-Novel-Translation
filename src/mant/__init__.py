"""mant：基于大数据与多智能体协作的网络小说自主翻译系统。

Multi-Agent Novel Translation —— 多智能体分工（调度 / 术语 / 翻译 / 审校 /
润色 / QA 终审）+ LangGraph 状态机（QA 不达标携带批注回退返工的回环）
+ 记忆与数据层（术语库 / 小说圣经 / 翻译记忆库 TM / FAISS 向量检索 RAG）。

包结构（骨架并行搭建中）：
    mant.llm       LLM 客户端封装（fast / strong 双档）
    mant.agents    多智能体基类与各角色 Agent
    mant.memory    记忆与数据层门面 MemoryHub 及数据模型
    mant.workflow  LangGraph 状态机与 TranslationState
    mant.pipeline  M1 离线语料管道（采集→清洗→句对齐→术语抽取）
    mant.cli       命令行入口

约定：所有第三方依赖一律延迟导入（函数内 import 或 try/except 降级），
保证仅用 stdlib + numpy 的环境下 ``import mant`` 与 ``python -m unittest`` 可用。
"""

__version__ = "0.1.0"

__all__ = ["__version__"]
