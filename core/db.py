"""SQLite 访问层与幂等迁移。

约束（PRD M7.6 / M9.7 / M10.5）：
- 迁移必须幂等，可重复执行；
- 所有筛选字段建立索引；
- local 10k 词条规模下常用查询 < 200ms。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Sequence

from config import Config

_local = threading.local()

# ---------------------------------------------------------------- 建表语句

_TABLES: dict[str, str] = {
    # ---------------- M2 工作流 ----------------
    "workflows": """
        CREATE TABLE IF NOT EXISTS workflows (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL DEFAULT '',
            filename        TEXT NOT NULL UNIQUE,
            source_path     TEXT,
            node_count      INTEGER NOT NULL DEFAULT 0,
            node_types      TEXT NOT NULL DEFAULT '[]',
            positive_prompt TEXT NOT NULL DEFAULT '',
            negative_prompt TEXT NOT NULL DEFAULT '',
            synced_at       TEXT,
            updated_at      TEXT
        )
    """,
    # ---------------- M3 图库 ----------------
    "images": """
        CREATE TABLE IF NOT EXISTS images (
            id           TEXT PRIMARY KEY,
            filename     TEXT NOT NULL DEFAULT '',
            source_path  TEXT,
            gallery_path TEXT,
            width        INTEGER,
            height       INTEGER,
            size_bytes   INTEGER,
            prompt       TEXT NOT NULL DEFAULT '',
            workflow_link TEXT,
            created_at   TEXT,
            synced_at    TEXT
        )
    """,
    # ---------------- M4 同步日志 ----------------
    "sync_log": """
        CREATE TABLE IF NOT EXISTS sync_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            ts      TEXT NOT NULL,
            type    TEXT NOT NULL DEFAULT 'info',
            message TEXT NOT NULL DEFAULT '',
            status  TEXT NOT NULL DEFAULT 'info'
        )
    """,
    # ---------------- M6 扩写历史 ----------------
    "prompt_expansions": """
        CREATE TABLE IF NOT EXISTS prompt_expansions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            original_prompt   TEXT NOT NULL,
            expanded_positive TEXT NOT NULL DEFAULT '',
            expanded_negative TEXT NOT NULL DEFAULT '',
            profile           TEXT NOT NULL DEFAULT 'generic',
            intensity         TEXT NOT NULL DEFAULT 'balanced',
            provider          TEXT NOT NULL DEFAULT 'rules',
            template_id       TEXT,
            sections          TEXT NOT NULL DEFAULT '[]',
            additions         TEXT NOT NULL DEFAULT '[]',
            warnings          TEXT NOT NULL DEFAULT '[]',
            is_favorite       INTEGER NOT NULL DEFAULT 0,
            created_at        TEXT
        )
    """,
    # ---------------- M7 精品词条 ----------------
    "prompt_entries": """
        CREATE TABLE IF NOT EXISTS prompt_entries (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type         TEXT NOT NULL,
            dimension          TEXT NOT NULL,
            subcategory        TEXT NOT NULL DEFAULT '',
            title              TEXT NOT NULL,
            prompt_text        TEXT NOT NULL,
            negative_text      TEXT,
            language           TEXT NOT NULL DEFAULT 'zh',
            model_profiles     TEXT NOT NULL DEFAULT '[]',
            tags               TEXT NOT NULL DEFAULT '[]',
            description        TEXT NOT NULL DEFAULT '',
            source_type        TEXT NOT NULL DEFAULT 'builtin',
            source_ref         TEXT,
            quality_score      INTEGER NOT NULL DEFAULT 80,
            content_fingerprint TEXT NOT NULL UNIQUE,
            is_favorite        INTEGER NOT NULL DEFAULT 0,
            is_hidden          INTEGER NOT NULL DEFAULT 0,
            use_count          INTEGER NOT NULL DEFAULT 0,
            last_used_at       TEXT,
            created_at         TEXT,
            updated_at         TEXT
        )
    """,
    # ---------------- M7 候选审核 ----------------
    "prompt_candidates": """
        CREATE TABLE IF NOT EXISTS prompt_candidates (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type          TEXT NOT NULL,
            raw_text            TEXT NOT NULL,
            normalized_text     TEXT NOT NULL,
            suggested_dimension TEXT NOT NULL DEFAULT '',
            suggested_subcategory TEXT NOT NULL DEFAULT '',
            source_ref          TEXT,
            confidence          REAL NOT NULL DEFAULT 0.0,
            quality_score       INTEGER NOT NULL DEFAULT 0,
            occurrence_count    INTEGER NOT NULL DEFAULT 1,
            review_status       TEXT NOT NULL DEFAULT 'pending',
            content_fingerprint TEXT NOT NULL UNIQUE,
            first_seen_at       TEXT,
            last_seen_at        TEXT,
            reviewed_at         TEXT
        )
    """,
    # ---------------- M7 生成学习链路 ----------------
    "generation_prompt_usage": """
        CREATE TABLE IF NOT EXISTS generation_prompt_usage (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            media_type         TEXT NOT NULL DEFAULT 'image',
            workflow_id        TEXT,
            model_profile      TEXT,
            original_prompt    TEXT NOT NULL DEFAULT '',
            structured_sections TEXT NOT NULL DEFAULT '[]',
            output_ref         TEXT,
            status             TEXT NOT NULL DEFAULT 'pending',
            created_at         TEXT,
            completed_at       TEXT,
            learned_at         TEXT
        )
    """,
    "prompt_ignored_fingerprints": """
        CREATE TABLE IF NOT EXISTS prompt_ignored_fingerprints (
            content_fingerprint TEXT PRIMARY KEY,
            reason              TEXT NOT NULL DEFAULT '',
            created_at          TEXT
        )
    """,
    # ---------------- M9 图片反推 ----------------
    "prompt_reverse_jobs": """
        CREATE TABLE IF NOT EXISTS prompt_reverse_jobs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id          TEXT,
            upload_ref        TEXT,
            content_hash      TEXT,
            source_mode       TEXT NOT NULL DEFAULT 'auto',
            provider          TEXT NOT NULL DEFAULT '',
            provider_model    TEXT NOT NULL DEFAULT '',
            model_profile     TEXT NOT NULL DEFAULT 'generic',
            precision_level   TEXT NOT NULL DEFAULT 'standard',
            status            TEXT NOT NULL DEFAULT 'pending',
            stage             TEXT NOT NULL DEFAULT 'queued',
            source_type       TEXT,
            raw_result        TEXT,
            structured_result TEXT,
            edited_result     TEXT,
            error_code        TEXT,
            duration_ms       INTEGER,
            created_at        TEXT,
            completed_at      TEXT,
            saved_at          TEXT
        )
    """,
    "prompt_reverse_consents": """
        CREATE TABLE IF NOT EXISTS prompt_reverse_consents (
            provider_key        TEXT PRIMARY KEY,
            consented_at        TEXT,
            provider_fingerprint TEXT
        )
    """,
    "prompt_entry_aliases": """
        CREATE TABLE IF NOT EXISTS prompt_entry_aliases (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt_entry_id  INTEGER NOT NULL,
            alias_text       TEXT NOT NULL,
            normalized_alias TEXT NOT NULL,
            source_type      TEXT NOT NULL DEFAULT '',
            source_ref       TEXT,
            created_at       TEXT,
            UNIQUE(prompt_entry_id, normalized_alias)
        )
    """,
    # ---------------- M10 Provider 偏好 ----------------
    "prompt_provider_preferences": """
        CREATE TABLE IF NOT EXISTS prompt_provider_preferences (
            provider_key TEXT PRIMARY KEY,
            is_default   INTEGER NOT NULL DEFAULT 0,
            updated_at   TEXT
        )
    """,
    # ---------------- M12 漫画工作室 ----------------
    "comic_projects": """
        CREATE TABLE IF NOT EXISTS comic_projects (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT NOT NULL,
            description      TEXT NOT NULL DEFAULT '',
            cover_image      TEXT NOT NULL DEFAULT '',
            status           TEXT NOT NULL DEFAULT 'draft',
            default_workflow TEXT NOT NULL DEFAULT '',
            global_params    TEXT NOT NULL DEFAULT '{}',
            pages_per_chapter INTEGER NOT NULL DEFAULT 1,
            style_preset     TEXT NOT NULL DEFAULT 'none',
            created_at       TEXT,
            updated_at       TEXT
        )
    """,
    "comic_chapters": """
        CREATE TABLE IF NOT EXISTS comic_chapters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            title       TEXT NOT NULL,
            order_idx   INTEGER NOT NULL DEFAULT 0,
            summary     TEXT NOT NULL DEFAULT '',
            created_at  TEXT,
            updated_at  TEXT
        )
    """,
    "comic_pages": """
        CREATE TABLE IF NOT EXISTS comic_pages (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id      INTEGER NOT NULL,
            chapter_id       INTEGER NOT NULL,
            title            TEXT NOT NULL DEFAULT '',
            order_idx        INTEGER NOT NULL DEFAULT 0,
            prompt_text      TEXT NOT NULL DEFAULT '',
            negative_text    TEXT NOT NULL DEFAULT '',
            workflow_filename TEXT NOT NULL DEFAULT '',
            seed             INTEGER,
            status           TEXT NOT NULL DEFAULT 'pending',
            image_path       TEXT NOT NULL DEFAULT '',
            last_job_id      INTEGER,
            base_prompt      TEXT NOT NULL DEFAULT '',
            base_negative    TEXT NOT NULL DEFAULT '',
            shot_note        TEXT NOT NULL DEFAULT '',
            created_at       TEXT,
            updated_at       TEXT
        )
    """,
    "comic_jobs": """
        CREATE TABLE IF NOT EXISTS comic_jobs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id     INTEGER NOT NULL,
            chapter_id  INTEGER NOT NULL,
            project_id  INTEGER NOT NULL,
            prompt_id   TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'queued',
            stage       TEXT NOT NULL DEFAULT 'queued',
            error_code  TEXT NOT NULL DEFAULT '',
            output_path TEXT NOT NULL DEFAULT '',
            attempt     INTEGER NOT NULL DEFAULT 0,
            error_message TEXT NOT NULL DEFAULT '',
            created_at  TEXT,
            finished_at TEXT
        )
    """,
    # M12 人物一致性：项目级「角色卡」，为每一页注入完全相同的角色锚点描述
    "comic_characters": """
        CREATE TABLE IF NOT EXISTS comic_characters (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL,
            name        TEXT NOT NULL,
            aliases     TEXT NOT NULL DEFAULT '',
            appearance  TEXT NOT NULL DEFAULT '',
            outfit      TEXT NOT NULL DEFAULT '',
            palette     TEXT NOT NULL DEFAULT '',
            negative    TEXT NOT NULL DEFAULT '',
            seed_offset INTEGER NOT NULL DEFAULT 0,
            is_main     INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT,
            updated_at  TEXT
        )
    """,
    # ---------------- M13 演员库 ----------------
    # 全局「演员」花名册：跨漫画项目共享的人物一致性基准源。
    # 与 comic_characters（项目级角色卡）的分工：
    #   - actors        = 演员本人，一次定妆、多部漫画复用（无 project_id）
    #   - comic_characters = 该演员在某部漫画里的角色（有 project_id）
    # 演员的 appearance/outfit/palette/negative 是权威定妆信息，
    # 角色可从演员继承，改演员即可批量校正所有关联角色。
    "actors": """
        CREATE TABLE IF NOT EXISTS actors (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            aliases       TEXT NOT NULL DEFAULT '',
            appearance    TEXT NOT NULL DEFAULT '',
            outfit        TEXT NOT NULL DEFAULT '',
            palette       TEXT NOT NULL DEFAULT '',
            negative      TEXT NOT NULL DEFAULT '',
            seed_offset   INTEGER NOT NULL DEFAULT 0,
            base_seed     INTEGER,
            seed          INTEGER,
            notes         TEXT NOT NULL DEFAULT '',
            image_id      TEXT,
            image_path    TEXT NOT NULL DEFAULT '',
            workflow      TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'ready',
            error_message TEXT NOT NULL DEFAULT '',
            source_type   TEXT NOT NULL DEFAULT 'manual',
            source_char_id    INTEGER,
            source_project_id INTEGER,
            use_count     INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT,
            updated_at    TEXT
        )
    """,
    # 演员 ↔ 漫画角色关系。一个演员可出演多部漫画的多个角色；
    # 一个角色只应绑定一个演员（由 UNIQUE(character_id) 保证），
    # 否则「以演员为基准保持一致性」会出现两个互相冲突的基准。
    "actor_character_links": """
        CREATE TABLE IF NOT EXISTS actor_character_links (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_id     INTEGER NOT NULL,
            character_id INTEGER NOT NULL UNIQUE,
            project_id   INTEGER NOT NULL DEFAULT 0,
            role_note    TEXT NOT NULL DEFAULT '',
            created_at   TEXT
        )
    """,
    # ---------------- M14 组图 ----------------
    # 组图（Shot Group）：同一人物做**连贯动作**的一组图，出完可连播成动画（像 GIF）。
    # 与 comic_pages 的分工：
    #   - comic_pages = 漫画分镜，每一页是**不同场景**的叙事画面，页数 = 剧情节奏；
    #   - shot_frames = 组图的每一帧，同一场景、同一人物，**只有动作在变**。
    # 因此组图不复用 comic_pages —— 硬塞进去会让「分镜页数」和「动作帧数」两个
    # 语义互相污染，也会让分镜的剧情扩写逻辑误伤动作帧。
    #
    # 「强制预设提示词」是本模块的核心：preset_prefix / preset_suffix /
    # preset_negative 由**组图**统一提供，帧只提供 ``action_text``（这一帧做什么动作）。
    # 帧表里虽然存了 prompt_text，但它**恒由预设拼装而来**（refresh-prompts 会用
    # 当前预设覆盖全部帧），单帧改不动 —— 这正是「强制」的含义，也是连播时
    # 人物不漂、画风不跳的根本保障。
    "shot_groups": """
        CREATE TABLE IF NOT EXISTS shot_groups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL DEFAULT '',
            actor_id        INTEGER,
            character_id    INTEGER,
            description     TEXT NOT NULL DEFAULT '',
            preset_prefix   TEXT NOT NULL DEFAULT '',
            preset_suffix   TEXT NOT NULL DEFAULT '',
            preset_negative TEXT NOT NULL DEFAULT '',
            anchor_override TEXT NOT NULL DEFAULT '',
            workflow        TEXT NOT NULL DEFAULT '',
            base_seed       INTEGER,
            seed_step       INTEGER NOT NULL DEFAULT 0,
            frame_interval  INTEGER NOT NULL DEFAULT 300,
            loop_play       INTEGER NOT NULL DEFAULT 1,
            status          TEXT NOT NULL DEFAULT 'idle',
            frame_count     INTEGER NOT NULL DEFAULT 0,
            done_count      INTEGER NOT NULL DEFAULT 0,
            cover_path      TEXT NOT NULL DEFAULT '',
            last_error      TEXT NOT NULL DEFAULT '',
            created_at      TEXT,
            updated_at      TEXT
        )
    """,
    # 组图的每一帧。``order_idx`` 就是连播顺序，允许重复（由 id 兜底保证稳定次序）。
    "shot_frames": """
        CREATE TABLE IF NOT EXISTS shot_frames (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id      INTEGER NOT NULL,
            order_idx     INTEGER NOT NULL DEFAULT 0,
            action_text   TEXT NOT NULL DEFAULT '',
            prompt_text   TEXT NOT NULL DEFAULT '',
            negative_text TEXT NOT NULL DEFAULT '',
            seed          INTEGER,
            workflow      TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'pending',
            image_path    TEXT NOT NULL DEFAULT '',
            image_id      TEXT,
            error_message TEXT NOT NULL DEFAULT '',
            created_at    TEXT,
            updated_at    TEXT
        )
    """,
    # M15 · 视频转序列帧 / GIF。
    #
    # 为什么**不为每一帧建表**：视频帧是「一次生成、整体播放」的批量产物，
    # 不像组图帧需要逐帧改动作 / 重生成 / 排序。建表只会多一张要维护的表，
    # 而列目录（`frames_dir` 下的 frame_NNNN.jpg）天然有序、零维护成本。
    # 真需要单帧操作时再补表也不迟——别为「可能有用」预先付复杂度。
    #
    # ``status``：idle（刚导入未抽帧）/ extracting（正在抽）/ ready（已抽帧）
    # / failed。GIF 是**可选产物**，抽完帧后单独触发（``gif_path`` 为空即未生成）。
    "video_clips": """
        CREATE TABLE IF NOT EXISTS video_clips (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            name         TEXT NOT NULL DEFAULT '',
            source_path  TEXT NOT NULL DEFAULT '',
            duration     REAL NOT NULL DEFAULT 0,
            width        INTEGER NOT NULL DEFAULT 0,
            height       INTEGER NOT NULL DEFAULT 0,
            src_fps      REAL NOT NULL DEFAULT 0,
            frame_count  INTEGER NOT NULL DEFAULT 0,
            fps          INTEGER NOT NULL DEFAULT 8,
            max_frames   INTEGER NOT NULL DEFAULT 300,
            scale_width  INTEGER NOT NULL DEFAULT 480,
            start_sec    REAL,
            end_sec      REAL,
            frames_dir   TEXT NOT NULL DEFAULT '',
            gif_path     TEXT NOT NULL DEFAULT '',
            gif_bytes    INTEGER NOT NULL DEFAULT 0,
            status       TEXT NOT NULL DEFAULT 'idle',
            last_error   TEXT NOT NULL DEFAULT '',
            created_at   TEXT,
            updated_at   TEXT
        )
    """,
    #
    # M16 视频转绘：视频 → 逐帧提姿态（DWPose）→ ControlNet 逐帧重绘 → 组图 + 无背景序列帧。
    # 一次「转绘任务」= 一个 video_paint_jobs 行 + N 个 video_paint_frames 行，
    # 并在 shot_groups / shot_frames 里同步建一份组图，从而直接出现在「组图管理」并可播放。
    #
    # 拆成两张表而不是塞进 video_clips：抽帧参数、生成参数、去背景参数是**转绘任务**的属性，
    # 与「源视频」无关；同一个视频可以以不同提示词/底模跑多个转绘任务，互不覆盖。
    "video_paint_jobs": """
        CREATE TABLE IF NOT EXISTS video_paint_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT NOT NULL DEFAULT '',
            clip_id         INTEGER NOT NULL DEFAULT 0,
            group_id        INTEGER NOT NULL DEFAULT 0,
            reference_rel   TEXT NOT NULL DEFAULT '',
            reference_name  TEXT NOT NULL DEFAULT '',
            prompt          TEXT NOT NULL DEFAULT '',
            negative        TEXT NOT NULL DEFAULT '',
            checkpoint      TEXT NOT NULL DEFAULT '',
            controlnet      TEXT NOT NULL DEFAULT '',
            pose_mode       TEXT NOT NULL DEFAULT 'dwpose',
            pose_resolution INTEGER NOT NULL DEFAULT 512,
            steps           INTEGER NOT NULL DEFAULT 28,
            cfg             REAL NOT NULL DEFAULT 6.5,
            controlnet_strength REAL NOT NULL DEFAULT 1.15,
            ipadapter_weight REAL NOT NULL DEFAULT 0.85,
            faceidv2_weight REAL NOT NULL DEFAULT 0.85,
            width           INTEGER NOT NULL DEFAULT 896,
            height          INTEGER NOT NULL DEFAULT 1152,
            base_seed       INTEGER NOT NULL DEFAULT 0,
            seed_step       INTEGER NOT NULL DEFAULT 0,
            fps             INTEGER NOT NULL DEFAULT 8,
            max_frames      INTEGER NOT NULL DEFAULT 4,
            scale_width     INTEGER NOT NULL DEFAULT 480,
            start_sec       REAL,
            end_sec         REAL,
            frame_count     INTEGER NOT NULL DEFAULT 0,
            pose_count      INTEGER NOT NULL DEFAULT 0,
            done_count      INTEGER NOT NULL DEFAULT 0,
            status          TEXT NOT NULL DEFAULT 'idle',
            last_error      TEXT NOT NULL DEFAULT '',
            has_nobg        INTEGER NOT NULL DEFAULT 0,
            bg_mode         TEXT NOT NULL DEFAULT 'colorkey',
            bg_color        TEXT NOT NULL DEFAULT '',
            bg_similarity   REAL,
            bg_blend        REAL,
            bg_seed         TEXT NOT NULL DEFAULT '',
            created_at      TEXT,
            updated_at      TEXT
        )
    """,
    # 逐帧产物：frame_name 是源帧文件名，pose_path 是提姿态得到的骨架图，
    # image_path 是重绘结果（同时写进 shot_frames 以便组图管理直接消费），
    # nobg_path 是去背景后的透明序列帧。三段状态机：pending → posed → done/failed。
    "video_paint_frames": """
        CREATE TABLE IF NOT EXISTS video_paint_frames (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        INTEGER NOT NULL,
            order_idx     INTEGER NOT NULL,
            frame_name    TEXT NOT NULL DEFAULT '',
            pose_path     TEXT NOT NULL DEFAULT '',
            image_path    TEXT NOT NULL DEFAULT '',
            nobg_path     TEXT NOT NULL DEFAULT '',
            shot_frame_id INTEGER NOT NULL DEFAULT 0,
            seed          INTEGER NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'pending',
            error_message TEXT NOT NULL DEFAULT '',
            pose_score    REAL,
            generation_attempts INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT,
            updated_at    TEXT
        )
    """,
}

_INDEXES: list[tuple[str, str]] = [
    ("idx_workflows_synced", "CREATE INDEX IF NOT EXISTS idx_workflows_synced ON workflows(synced_at)"),
    ("idx_images_created", "CREATE INDEX IF NOT EXISTS idx_images_created ON images(created_at)"),
    ("idx_images_created_desc", "CREATE INDEX IF NOT EXISTS idx_images_created_desc ON images(created_at DESC)"),
    ("idx_sync_log_ts", "CREATE INDEX IF NOT EXISTS idx_sync_log_ts ON sync_log(ts DESC)"),
    ("idx_expansions_created", "CREATE INDEX IF NOT EXISTS idx_expansions_created ON prompt_expansions(created_at DESC)"),
    ("idx_expansions_fav", "CREATE INDEX IF NOT EXISTS idx_expansions_fav ON prompt_expansions(is_favorite)"),
    ("idx_entries_media", "CREATE INDEX IF NOT EXISTS idx_entries_media ON prompt_entries(media_type)"),
    ("idx_entries_media_dim", "CREATE INDEX IF NOT EXISTS idx_entries_media_dim ON prompt_entries(media_type, dimension)"),
    (
        "idx_entries_media_dim_sub",
        "CREATE INDEX IF NOT EXISTS idx_entries_media_dim_sub ON prompt_entries(media_type, dimension, subcategory)",
    ),
    ("idx_entries_source", "CREATE INDEX IF NOT EXISTS idx_entries_source ON prompt_entries(source_type)"),
    ("idx_entries_favorite", "CREATE INDEX IF NOT EXISTS idx_entries_favorite ON prompt_entries(is_favorite)"),
    ("idx_entries_hidden", "CREATE INDEX IF NOT EXISTS idx_entries_hidden ON prompt_entries(is_hidden)"),
    ("idx_entries_used", "CREATE INDEX IF NOT EXISTS idx_entries_used ON prompt_entries(use_count DESC, last_used_at DESC)"),
    ("idx_candidates_status", "CREATE INDEX IF NOT EXISTS idx_candidates_status ON prompt_candidates(review_status)"),
    ("idx_candidates_media", "CREATE INDEX IF NOT EXISTS idx_candidates_media ON prompt_candidates(media_type, review_status)"),
    ("idx_usage_status", "CREATE INDEX IF NOT EXISTS idx_usage_status ON generation_prompt_usage(status)"),
    ("idx_usage_created", "CREATE INDEX IF NOT EXISTS idx_usage_created ON generation_prompt_usage(created_at DESC)"),
    ("idx_reverse_status", "CREATE INDEX IF NOT EXISTS idx_reverse_status ON prompt_reverse_jobs(status)"),
    ("idx_reverse_created", "CREATE INDEX IF NOT EXISTS idx_reverse_created ON prompt_reverse_jobs(created_at DESC)"),
    ("idx_reverse_provider", "CREATE INDEX IF NOT EXISTS idx_reverse_provider ON prompt_reverse_jobs(provider)"),
    ("idx_reverse_hash", "CREATE INDEX IF NOT EXISTS idx_reverse_hash ON prompt_reverse_jobs(content_hash)"),
    ("idx_alias_entry", "CREATE INDEX IF NOT EXISTS idx_alias_entry ON prompt_entry_aliases(prompt_entry_id)"),
    ("idx_alias_norm", "CREATE INDEX IF NOT EXISTS idx_alias_norm ON prompt_entry_aliases(normalized_alias)"),
    ("idx_comic_chapters_project", "CREATE INDEX IF NOT EXISTS idx_comic_chapters_project ON comic_chapters(project_id)"),
    ("idx_comic_pages_chapter", "CREATE INDEX IF NOT EXISTS idx_comic_pages_chapter ON comic_pages(chapter_id)"),
    ("idx_comic_pages_project", "CREATE INDEX IF NOT EXISTS idx_comic_pages_project ON comic_pages(project_id)"),
    ("idx_comic_pages_status", "CREATE INDEX IF NOT EXISTS idx_comic_pages_status ON comic_pages(status)"),
    ("idx_comic_jobs_status", "CREATE INDEX IF NOT EXISTS idx_comic_jobs_status ON comic_jobs(status)"),
    ("idx_comic_jobs_project", "CREATE INDEX IF NOT EXISTS idx_comic_jobs_project ON comic_jobs(project_id)"),
    ("idx_comic_jobs_page", "CREATE INDEX IF NOT EXISTS idx_comic_jobs_page ON comic_jobs(page_id)"),
    ("idx_comic_characters_project", "CREATE INDEX IF NOT EXISTS idx_comic_characters_project ON comic_characters(project_id)"),
    ("idx_comic_characters_name", "CREATE INDEX IF NOT EXISTS idx_comic_characters_name ON comic_characters(project_id, name)"),
    # M13 演员库
    ("idx_actors_name", "CREATE INDEX IF NOT EXISTS idx_actors_name ON actors(name)"),
    ("idx_actors_status", "CREATE INDEX IF NOT EXISTS idx_actors_status ON actors(status)"),
    ("idx_actors_updated", "CREATE INDEX IF NOT EXISTS idx_actors_updated ON actors(updated_at DESC)"),
    ("idx_actor_links_actor", "CREATE INDEX IF NOT EXISTS idx_actor_links_actor ON actor_character_links(actor_id)"),
    ("idx_actor_links_char", "CREATE INDEX IF NOT EXISTS idx_actor_links_char ON actor_character_links(character_id)"),
    ("idx_actor_links_project", "CREATE INDEX IF NOT EXISTS idx_actor_links_project ON actor_character_links(project_id)"),
    # M14 组图
    ("idx_shot_groups_updated", "CREATE INDEX IF NOT EXISTS idx_shot_groups_updated ON shot_groups(updated_at DESC)"),
    ("idx_shot_groups_status", "CREATE INDEX IF NOT EXISTS idx_shot_groups_status ON shot_groups(status)"),
    ("idx_shot_groups_actor", "CREATE INDEX IF NOT EXISTS idx_shot_groups_actor ON shot_groups(actor_id)"),
    ("idx_shot_frames_group", "CREATE INDEX IF NOT EXISTS idx_shot_frames_group ON shot_frames(group_id, order_idx)"),
    ("idx_shot_frames_status", "CREATE INDEX IF NOT EXISTS idx_shot_frames_status ON shot_frames(group_id, status)"),
    # M15 视频转序列帧 / GIF
    ("idx_video_clips_updated", "CREATE INDEX IF NOT EXISTS idx_video_clips_updated ON video_clips(updated_at DESC)"),
    ("idx_video_clips_status", "CREATE INDEX IF NOT EXISTS idx_video_clips_status ON video_clips(status)"),
    # M16 视频转绘
    ("idx_video_paint_jobs_clip", "CREATE INDEX IF NOT EXISTS idx_video_paint_jobs_clip ON video_paint_jobs(clip_id)"),
    ("idx_video_paint_jobs_status", "CREATE INDEX IF NOT EXISTS idx_video_paint_jobs_status ON video_paint_jobs(status)"),
    ("idx_video_paint_jobs_updated", "CREATE INDEX IF NOT EXISTS idx_video_paint_jobs_updated ON video_paint_jobs(updated_at DESC)"),
    ("idx_video_paint_frames_job", "CREATE INDEX IF NOT EXISTS idx_video_paint_frames_job ON video_paint_frames(job_id, order_idx)"),
    ("idx_video_paint_frames_status", "CREATE INDEX IF NOT EXISTS idx_video_paint_frames_status ON video_paint_frames(job_id, status)"),
]

# 幂等补列：只在列缺失时执行
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("prompt_entries", "is_hidden", "ALTER TABLE prompt_entries ADD COLUMN is_hidden INTEGER NOT NULL DEFAULT 0"),
    ("prompt_entries", "negative_text", "ALTER TABLE prompt_entries ADD COLUMN negative_text TEXT"),
    ("prompt_reverse_jobs", "saved_at", "ALTER TABLE prompt_reverse_jobs ADD COLUMN saved_at TEXT"),
    ("prompt_expansions", "template_id", "ALTER TABLE prompt_expansions ADD COLUMN template_id TEXT"),
    # M12 分镜工作流：漫画的世界观与剧情摘要（可编辑，供自动分镜生成消费）
    ("comic_projects", "worldview", "ALTER TABLE comic_projects ADD COLUMN worldview TEXT NOT NULL DEFAULT ''"),
    ("comic_projects", "plot_summary", "ALTER TABLE comic_projects ADD COLUMN plot_summary TEXT NOT NULL DEFAULT ''"),
    # M12 分镜工作流：每章出图页数（出图数量），控制每集拆出的漫画页数
    ("comic_projects", "pages_per_chapter", "ALTER TABLE comic_projects ADD COLUMN pages_per_chapter INTEGER NOT NULL DEFAULT 1"),
    # M12 分镜工作流：出图风格（统一规范每一集、每一页的画面风格）
    ("comic_projects", "style_preset", "ALTER TABLE comic_projects ADD COLUMN style_preset TEXT NOT NULL DEFAULT 'none'"),
    # M12 人物一致性：项目基准种子，页级种子由它 + 角色/章节确定性派生
    ("comic_projects", "base_seed", "ALTER TABLE comic_projects ADD COLUMN base_seed INTEGER"),
    # M12 人物一致性：该页涉及的角色名（逗号分隔），用于注入角色锚点
    ("comic_pages", "character_names", "ALTER TABLE comic_pages ADD COLUMN character_names TEXT NOT NULL DEFAULT ''"),
    # M12 出图诊断：页级可读失败原因（与 job 的 error_message 同步）
    ("comic_pages", "error_message", "ALTER TABLE comic_pages ADD COLUMN error_message TEXT NOT NULL DEFAULT ''"),
    # M12 出图诊断：任务重试次数与可读失败原因
    ("comic_jobs", "attempt", "ALTER TABLE comic_jobs ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0"),
    ("comic_jobs", "error_message", "ALTER TABLE comic_jobs ADD COLUMN error_message TEXT NOT NULL DEFAULT ''"),
    # M12 出图诊断：重试退避时间点（未到点不重新入队，避免失败风暴）
    ("comic_jobs", "next_retry_at", "ALTER TABLE comic_jobs ADD COLUMN next_retry_at TEXT"),
    # M12 提示词可重建：保存「扩写后、注入角色锚点与风格词之前」的原始提示词，
    # 使改角色卡 / 换风格后能一键重算全部页提示词，而不必重新拆分剧情。
    ("comic_pages", "base_prompt", "ALTER TABLE comic_pages ADD COLUMN base_prompt TEXT NOT NULL DEFAULT ''"),
    ("comic_pages", "base_negative", "ALTER TABLE comic_pages ADD COLUMN base_negative TEXT NOT NULL DEFAULT ''"),
    # M12 提示词可重建：该页的镜头景别（如「特写」），重算提示词时保留
    ("comic_pages", "shot_note", "ALTER TABLE comic_pages ADD COLUMN shot_note TEXT NOT NULL DEFAULT ''"),
    # M12 人物一致性：主角标记。主角在该项目每一页都注入锚点，不依赖单页文本是否提到名字
    ("comic_characters", "is_main", "ALTER TABLE comic_characters ADD COLUMN is_main INTEGER NOT NULL DEFAULT 0"),
    # M12 角色管理：关联图库样板图片（导入图库图片作为角色），编辑框外显样板图
    ("comic_characters", "image_id", "ALTER TABLE comic_characters ADD COLUMN image_id TEXT"),
    # M13 演员库：保存「最近一次实际出图所用的种子」，使 randomize=True 的脸能被稳定展示与复现，
    # 同时避免每次重生成都覆盖 base_seed（base_seed 是确定性基准，必须保持稳定才能保证同一张脸）
    ("actors", "seed", "ALTER TABLE actors ADD COLUMN seed INTEGER"),
    # M12 产出图回流图库：登记进 images 后的图库 id，打通「反推提示词 / 图库深链」。
    # 用 NULL 而不是 '' 表示「尚未登记」，便于 WHERE image_id IS NULL 精确补登记。
    ("comic_pages", "image_id", "ALTER TABLE comic_pages ADD COLUMN image_id TEXT"),
    # M12 单页重扩写：剧情原文（扩写前的镜头描述）。
    # 存了它才能对单页重跑一次「扩写」而不是只能重套锚点与风格。
    ("comic_pages", "beat_text", "ALTER TABLE comic_pages ADD COLUMN beat_text TEXT NOT NULL DEFAULT ''"),
    # M15 去背景：是否已生成透明帧变体 + 抠图参数（存下来才能复现与二次微调）
    ("video_clips", "has_nobg", "ALTER TABLE video_clips ADD COLUMN has_nobg INTEGER NOT NULL DEFAULT 0"),
    ("video_clips", "bg_color", "ALTER TABLE video_clips ADD COLUMN bg_color TEXT NOT NULL DEFAULT ''"),
    ("video_clips", "bg_similarity", "ALTER TABLE video_clips ADD COLUMN bg_similarity REAL"),
    ("video_clips", "bg_blend", "ALTER TABLE video_clips ADD COLUMN bg_blend REAL"),
    # M15 序列帧拼图：是否已拼过大图 + 拼图列数
    ("video_clips", "has_sheet", "ALTER TABLE video_clips ADD COLUMN has_sheet INTEGER NOT NULL DEFAULT 0"),
    ("video_clips", "sheet_cols", "ALTER TABLE video_clips ADD COLUMN sheet_cols INTEGER"),
    # M15 v3 双色抠图：第二把钥匙色 + 各自的容差/羽化
    ("video_clips", "bg_color2", "ALTER TABLE video_clips ADD COLUMN bg_color2 TEXT NOT NULL DEFAULT ''"),
    ("video_clips", "bg_similarity2", "ALTER TABLE video_clips ADD COLUMN bg_similarity2 REAL"),
    ("video_clips", "bg_blend2", "ALTER TABLE video_clips ADD COLUMN bg_blend2 REAL"),
    # M15 v4 连通抠图：去背景模式（colorkey=按颜色整图；flood=只抠选点连通背景）+ 种子点坐标
    ("video_clips", "bg_mode", "ALTER TABLE video_clips ADD COLUMN bg_mode TEXT NOT NULL DEFAULT 'colorkey'"),
    ("video_clips", "bg_seed", "ALTER TABLE video_clips ADD COLUMN bg_seed TEXT NOT NULL DEFAULT ''"),
    # M16 严格骨架验收：只把通过生成后 DWPose 复检的帧标成 done。
    ("video_paint_frames", "pose_score", "ALTER TABLE video_paint_frames ADD COLUMN pose_score REAL"),
    ("video_paint_frames", "generation_attempts", "ALTER TABLE video_paint_frames ADD COLUMN generation_attempts INTEGER NOT NULL DEFAULT 0"),
]


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def now() -> str:
    """统一时间戳格式（本地时间，字符串存储，便于排序与展示）。"""
    return _now()


# ---------------------------------------------------------------- 连接管理


def db_path() -> Path:
    return Path(Config.DB_PATH)


# 事务嵌套深度。跟连接一样是线程局部的——Flask 多线程模型下，
# 一个线程的事务不能影响另一个线程是否自提交。
# （不能挂在 sqlite3.Connection 上：它不允许设置自定义属性。）
_TX_DEPTH_ATTR = "tx_depth"


def get_conn() -> sqlite3.Connection:
    """线程本地连接。Flask 多线程模型下每个线程独立持有一个连接。"""
    conn: sqlite3.Connection | None = getattr(_local, "conn", None)
    if conn is None:
        db_path().parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path()), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    """事务上下文：正常退出提交，异常回滚并向上抛出。

    嵌套时只有最外层真正提交/回滚，内层退出时不动作，交给外层收口。

    注意与 :func:`execute` 的配合：``execute`` 默认每句自提交，但一旦处于
    ``tx()`` 内就不再提交（见 ``_TX_DEPTH_ATTR``），否则 ``tx()`` 形同虚设——
    半截写入已经落盘，后面的语句再失败也回滚不回来了。
    """
    conn = get_conn()
    depth = getattr(_local, _TX_DEPTH_ATTR, 0)
    setattr(_local, _TX_DEPTH_ATTR, depth + 1)
    try:
        if depth == 0:
            with conn:
                yield conn
        else:
            yield conn
    finally:
        setattr(_local, _TX_DEPTH_ATTR, depth)


def execute(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
    """执行一条写语句。事务外自提交；事务内交给 :func:`tx` 统一收口。"""
    conn = get_conn()
    cur = conn.execute(sql, params)
    if not getattr(_local, _TX_DEPTH_ATTR, 0):
        conn.commit()
    return cur


def query_all(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
    return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Row | None:
    return get_conn().execute(sql, params).fetchone()


def query_scalar(sql: str, params: Sequence[Any] | dict[str, Any] = (), default: Any = None) -> Any:
    row = query_one(sql, params)
    if row is None:
        return default
    return row[0]


# ---------------------------------------------------------------- 迁移


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate() -> list[str]:
    """执行全部幂等迁移，返回本次真正执行的动作列表（用于启动日志）。

    可重复执行；已存在的表/列/索引不会重复创建。
    """
    Config.ensure_dirs()
    conn = get_conn()
    actions: list[str] = []
    for name, ddl in _TABLES.items():
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        conn.execute(ddl)
        if exists is None:
            actions.append(f"create_table:{name}")
    for table, column, ddl in _COLUMN_MIGRATIONS:
        if table in _TABLES or _table_exists(conn, table):
            if column not in _table_columns(conn, table):
                conn.execute(ddl)
                actions.append(f"add_column:{table}.{column}")
    for name, ddl in _INDEXES:
        exists = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        conn.execute(ddl)
        if exists is None:
            actions.append(f"create_index:{name}")
    conn.commit()
    return actions


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return (
        conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


# ---------------------------------------------------------------- 小工具


def log_sync(message: str, level: str = "info", kind: str = "sync") -> None:
    """写入同步日志表（同时作为模块间统一的运行日志持久化入口）。"""
    try:
        execute(
            "INSERT INTO sync_log (ts, type, message, status) VALUES (?, ?, ?, ?)",
            (now(), kind, message[:500], level),
        )
    except sqlite3.Error:
        pass


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


def json_field(value: Any, default: Any = None) -> Any:
    """把数据库里的 JSON 文本安全解析为 Python 对象。"""
    if value is None or value == "":
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return default


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)
