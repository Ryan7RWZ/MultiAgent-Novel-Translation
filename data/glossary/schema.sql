-- ============================================================================
-- 术语库（Glossary）表结构定义
-- 项目：MultiAgent-Novel-Translation（包名 mant）
-- 说明：
--   1. 本文件是术语库 schema 的对外文档与迁移基准（供 DBA / 迁移脚本使用）；
--      运行时代码内嵌于 src/mant/memory/glossary.py 的建表语句须与本文件保持一致。
--   2. 默认存储为 SQLite（stdlib sqlite3），后续可平滑迁移至 Postgres
--      （字段类型均为通用类型，迁移成本低）。
-- ============================================================================

-- 术语表：一条记录 = 某作品下一个源语言术语的约定译法
CREATE TABLE IF NOT EXISTS terms (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,          -- 自增主键
    source      TEXT    NOT NULL,                           -- 源语言术语原文
    target      TEXT    NOT NULL,                           -- 约定译法（目标语言）
    category    TEXT    NOT NULL DEFAULT '',                -- 术语分类（人物/地名/功法/势力……）
    work_id     TEXT    NOT NULL,                           -- 所属作品 ID（术语库按作品隔离）
    confidence  REAL    NOT NULL DEFAULT 1.0                -- 置信度 0~1，人工确认置 1.0
        CHECK (confidence BETWEEN 0.0 AND 1.0),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))  -- 创建时间（UTC，ISO 格式）
);

-- 唯一索引 (source, work_id)：同一作品内同一源术语唯一，
-- 同时作为 GlossaryStore.upsert 的 ON CONFLICT 冲突键
CREATE UNIQUE INDEX IF NOT EXISTS uq_terms_source_work
    ON terms (source, work_id);

-- 按作品全量列出术语（list_by_work / 导出审阅）
CREATE INDEX IF NOT EXISTS idx_terms_work_id
    ON terms (work_id);

-- 按作品 + 分类过滤（术语表按类别导出、审校按类别抽查）
CREATE INDEX IF NOT EXISTS idx_terms_work_category
    ON terms (work_id, category);
