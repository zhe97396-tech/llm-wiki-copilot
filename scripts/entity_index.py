#!/usr/bin/env python3
"""
Entity Index for LLM Wiki: SQLite-based entity bridging for multi-hop retrieval.

Stores structured chunk→entity relationships extracted from wiki pages,
enabling "entity bridge" expansion during search — finding documents that
share entities with search hits, even when vector similarity alone misses them.

Three-table model:
  - entities: concept names + type + mention_count
  - chunks: wiki pages + event_text (one-sentence summary)
  - chunk_entities: many-to-many with role (core/mention)

Usage:
    python scripts/entity_index.py --rebuild          # Rebuild from all wiki pages
    python scripts/entity_index.py --stats             # Show index statistics
    python scripts/entity_index.py --check              # Consistency check
    python scripts/entity_index.py --bridge "认知Commons"  # Test bridge expansion
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import sys
from pathlib import Path

# Bootstrap shared utilities
sys.path.insert(0, str(Path(__file__).parent))
from _utils import (
    DATA_ROOT, DATA_DIR, WIKI_DIR, GITHUB_STARS_DIR,
    read_file, write_file, strip_frontmatter, extract_frontmatter_field,
    extract_wikilinks, all_wiki_pages, page_id, page_name,
    ensure_data_dir,
)

# ── Path Constants ────────────────────────────────────────────────────

ENTITY_DB = DATA_DIR / "entity_index.db"
CONCEPTS_DIR = WIKI_DIR / "concepts"
SUMMARIES_DIR = WIKI_DIR / "summaries"

# ── Role Inference ────────────────────────────────────────────────────
# Infer core/mention from document structure without extra LLM calls.

# Sections that indicate a concept is a core topic of the document
_CORE_SECTIONS = {
    "核心结论",
    "关键知识点",
    "核心概念",
    "核心主旨",
    "核心论点",
}


def _infer_roles(content: str, entity_names: list[str]) -> dict[str, str]:
    """Infer core/mention role for each entity based on document structure.

    Rules:
      - Entity appears in core sections (# 1. 核心结论 / # 2. 关键知识点) → core
      - Entity only appears in concept association section (# 4. 核心概念与关联) → mention
      - Default: mention (conservative — only clearly core entities get core role)
    """
    roles = {}
    body = strip_frontmatter(content)

    # Split into sections
    sections: dict[str, str] = {}
    current_section = ""
    current_text: list[str] = []

    for line in body.split("\n"):
        # Match markdown headings: # 1. xxx or ## 1. xxx
        m = re.match(r'^#{1,3}\s+\d*\.?\s*(.+)$', line)
        if m:
            if current_section:
                sections[current_section] = "\n".join(current_text)
            current_section = m.group(1).strip()
            current_text = []
        else:
            current_text.append(line)

    if current_section:
        sections[current_section] = "\n".join(current_text)

    # Build core text: content from core sections
    core_text = ""
    for section_name, section_text in sections.items():
        # Check if section name contains any core section keyword
        if any(kw in section_name for kw in _CORE_SECTIONS):
            core_text += section_text + "\n"

    for name in entity_names:
        # Entity appears in core sections → core
        if name in core_text:
            roles[name] = "core"
        else:
            roles[name] = "mention"

    return roles


def _extract_event_text(content: str) -> str:
    """Extract one-sentence summary from a wiki page.

    For summary pages: first line after '# 1. 核心结论' section.
    For concept pages: first paragraph (the definition).
    """
    body = strip_frontmatter(content)

    # Try to find "核心结论" section
    m = re.search(r'^#{1,3}\s+\d*\.?\s*核心结论\s*$', body, re.MULTILINE)
    if m:
        after = body[m.end():].strip()
        # Take first non-empty line
        for line in after.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                # Strip markdown bold/italic
                line = re.sub(r'\*{1,3}', '', line)
                line = re.sub(r'_{1,3}', '', line)
                return line[:200]

    # Fallback: first non-empty, non-heading paragraph
    for line in body.split("\n"):
        line = line.strip()
        if line and not line.startswith("#") and not line.startswith("---") and not line.startswith("```"):
            line = re.sub(r'\*{1,3}', '', line)
            line = re.sub(r'_{1,3}', '', line)
            return line[:200]

    return ""


def _extract_concept_names_from_summary(content: str) -> list[str]:
    """Extract concept names from a summary page's '# 4. 核心概念与关联' section.

    These are the [[wikilinks]] in that section, which correspond to
    wiki/concepts/ pages.
    """
    body = strip_frontmatter(content)

    # Find the concept association section
    m = re.search(r'^#{1,3}\s+\d*\.?\s*核心概念与关联', body, re.MULTILINE)
    if not m:
        # Fallback: extract all wikilinks from the whole document
        return extract_wikilinks(content)

    # Find the next section heading after this one
    after = body[m.end():]
    next_section = re.search(r'^#{1,3}\s+\d*\.?\s*\S', after, re.MULTILINE)
    if next_section:
        section_text = after[:next_section.start()]
    else:
        section_text = after

    return extract_wikilinks(section_text)


def _extract_concept_names_from_concept(content: str) -> list[str]:
    """Extract related concept names from a concept page.

    These are the [[wikilinks]] in the '来源' line at the bottom,
    plus any [[wikilinks]] in the body that point to other concepts.
    """
    links = extract_wikilinks(content)
    # Filter: only keep links that exist as concept pages
    concept_names = []
    for link in links:
        concept_path = WIKI_DIR / "concepts" / f"{link}.md"
        if concept_path.exists():
            concept_names.append(link)
    return concept_names


# ── Database Layer ────────────────────────────────────────────────────


def _get_conn() -> sqlite3.Connection:
    """Get a connection to the entity index database."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ENTITY_DB))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create entity_index.db and tables (idempotent)."""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            type TEXT NOT NULL DEFAULT 'concept',
            mention_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id TEXT NOT NULL UNIQUE,
            source_path TEXT NOT NULL,
            event_text TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS chunk_entities (
            chunk_id INTEGER NOT NULL,
            entity_id INTEGER NOT NULL,
            role TEXT NOT NULL DEFAULT 'mention',
            PRIMARY KEY (chunk_id, entity_id),
            FOREIGN KEY (chunk_id) REFERENCES chunks(id) ON DELETE CASCADE,
            FOREIGN KEY (entity_id) REFERENCES entities(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_chunk_entities_entity
            ON chunk_entities(entity_id);
        CREATE INDEX IF NOT EXISTS idx_chunk_entities_chunk
            ON chunk_entities(chunk_id);
        CREATE INDEX IF NOT EXISTS idx_entities_name
            ON entities(name);
        CREATE INDEX IF NOT EXISTS idx_chunks_page_id
            ON chunks(page_id);
    """)
    conn.commit()
    conn.close()


# ── Write Operations ─────────────────────────────────────────────────


def upsert_chunk(page_id_str: str, source_path: str, event_text: str) -> int:
    """Insert or update a chunk record. Returns the chunk's row id."""
    conn = _get_conn()
    try:
        # Check if chunk exists
        row = conn.execute(
            "SELECT id FROM chunks WHERE page_id = ?", (page_id_str,)
        ).fetchone()

        if row:
            chunk_id = row["id"]
            conn.execute(
                "UPDATE chunks SET source_path = ?, event_text = ? WHERE id = ?",
                (source_path, event_text, chunk_id),
            )
        else:
            cursor = conn.execute(
                "INSERT INTO chunks (page_id, source_path, event_text) VALUES (?, ?, ?)",
                (page_id_str, source_path, event_text),
            )
            chunk_id = cursor.lastrowid
            if chunk_id is None:
                raise RuntimeError(f"Failed to insert chunk: {page_id_str}")
            chunk_id = int(chunk_id)

        conn.commit()
        return chunk_id
    finally:
        conn.close()


def upsert_entities(entity_names: list[str], entity_type: str = "concept") -> dict[str, int]:
    """Insert entities or increment mention_count. Returns {name: entity_id}."""
    if not entity_names:
        return {}

    conn = _get_conn()
    try:
        result = {}
        for name in entity_names:
            row = conn.execute(
                "SELECT id, mention_count FROM entities WHERE name = ?", (name,)
            ).fetchone()

            if row:
                conn.execute(
                    "UPDATE entities SET mention_count = mention_count + 1 WHERE id = ?",
                    (row["id"],),
                )
                result[name] = row["id"]
            else:
                cursor = conn.execute(
                    "INSERT INTO entities (name, type, mention_count) VALUES (?, ?, 1)",
                    (name, entity_type),
                )
                result[name] = cursor.lastrowid

        conn.commit()
        return result
    finally:
        conn.close()


def _decrement_entity_mentions(conn: sqlite3.Connection, entity_ids: list[int]) -> None:
    """Decrement mention_count for entities; delete if reaches 0."""
    for eid in entity_ids:
        row = conn.execute(
            "SELECT mention_count FROM entities WHERE id = ?", (eid,)
        ).fetchone()
        if row and row["mention_count"] <= 1:
            conn.execute("DELETE FROM entities WHERE id = ?", (eid,))
        elif row:
            conn.execute(
                "UPDATE entities SET mention_count = mention_count - 1 WHERE id = ?",
                (eid,),
            )


def link_chunk_entities(
    chunk_page_id: str,
    entity_names: list[str],
    roles: dict[str, str] | None = None,
) -> None:
    """Link a chunk to its entities. Deletes old links first (with mention_count decrement)."""
    if not entity_names:
        return

    conn = _get_conn()
    try:
        # Get chunk id
        chunk_row = conn.execute(
            "SELECT id FROM chunks WHERE page_id = ?", (chunk_page_id,)
        ).fetchone()
        if not chunk_row:
            return
        chunk_id = chunk_row["id"]

        # Get old entity ids for mention_count decrement
        old_links = conn.execute(
            "SELECT entity_id FROM chunk_entities WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchall()
        old_entity_ids = [r["entity_id"] for r in old_links]

        # Delete old links
        conn.execute("DELETE FROM chunk_entities WHERE chunk_id = ?", (chunk_id,))

        # Decrement old entity mentions
        _decrement_entity_mentions(conn, old_entity_ids)

        # Insert new links
        roles = roles or {}
        for name in entity_names:
            entity_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (name,)
            ).fetchone()
            if not entity_row:
                continue

            role = roles.get(name, "mention")
            conn.execute(
                "INSERT INTO chunk_entities (chunk_id, entity_id, role) VALUES (?, ?, ?)",
                (chunk_id, entity_row["id"], role),
            )

        # Increment new entity mentions
        # (upsert_entities already incremented, but we deleted old and added new,
        #  so we need to increment for the new links)
        # Actually, upsert_entities is called BEFORE link_chunk_entities,
        # so mentions are already +1 for new entities. For existing entities
        # that were in old links AND new links, the net is 0 (decrement + already incremented).
        # For entities that are NEW in this link (not in old), they were already +1 by upsert.
        # For entities that are ONLY in old (removed), we decremented above. Correct.
        # But we need to increment for entities that appear in new links
        # because upsert_entities was called before we deleted old links.
        # The simplest fix: increment for all new entity ids.
        new_entity_ids = []
        for name in entity_names:
            entity_row = conn.execute(
                "SELECT id FROM entities WHERE name = ?", (name,)
            ).fetchone()
            if entity_row:
                new_entity_ids.append(entity_row["id"])

        for eid in new_entity_ids:
            conn.execute(
                "UPDATE entities SET mention_count = mention_count + 1 WHERE id = ?",
                (eid,),
            )

        conn.commit()
    finally:
        conn.close()


def index_page(
    page_id_str: str,
    source_path: str,
    event_text: str,
    entity_names: list[str],
    roles: dict[str, str] | None = None,
) -> None:
    """One-stop: upsert chunk + upsert entities + link chunk to entities."""
    upsert_chunk(page_id_str, source_path, event_text)
    upsert_entities(entity_names)
    link_chunk_entities(page_id_str, entity_names, roles)


def delete_chunk(page_id_str: str) -> None:
    """Remove a chunk and its entity links (with mention_count decrement)."""
    conn = _get_conn()
    try:
        chunk_row = conn.execute(
            "SELECT id FROM chunks WHERE page_id = ?", (page_id_str,)
        ).fetchone()
        if not chunk_row:
            return

        chunk_id = chunk_row["id"]

        # Get entity ids for decrement
        old_links = conn.execute(
            "SELECT entity_id FROM chunk_entities WHERE chunk_id = ?",
            (chunk_id,),
        ).fetchall()
        old_entity_ids = [r["entity_id"] for r in old_links]

        # Delete chunk (cascades to chunk_entities)
        conn.execute("DELETE FROM chunks WHERE id = ?", (chunk_id,))

        # Decrement entity mentions
        _decrement_entity_mentions(conn, old_entity_ids)

        conn.commit()
    finally:
        conn.close()


# ── Query Operations ─────────────────────────────────────────────────


def get_chunk_entities(page_ids: list[str]) -> dict[str, list[str]]:
    """Return {page_id: [entity_name, ...]} for given page ids."""
    if not page_ids:
        return {}

    conn = _get_conn()
    try:
        result = {}
        placeholders = ",".join("?" * len(page_ids))
        rows = conn.execute(f"""
            SELECT c.page_id, e.name
            FROM chunk_entities ce
            JOIN chunks c ON ce.chunk_id = c.id
            JOIN entities e ON ce.entity_id = e.id
            WHERE c.page_id IN ({placeholders})
        """, page_ids).fetchall()

        for row in rows:
            pid = row["page_id"]
            if pid not in result:
                result[pid] = []
            result[pid].append(row["name"])

        return result
    finally:
        conn.close()


def bridge_by_entities(
    seed_page_ids: list[str],
    seed_scores: dict[str, float],
    top_k: int = 5,
    decay: float = 0.4,
    exclude_types: set[str] | None = None,
) -> list[dict]:
    """Entity bridge: find chunks sharing entities with seed chunks.

    Three-layer filter:
      1. Shared ≥ 2 entities → bridge (strong)
      2. Shared 1 entity, both core → bridge (medium)
      3. Shared 1 entity, at least one mention → skip (noise)

    Scoring:
      bridge_score = max(seed_score) × decay × (shared_count / max_shared)
                     × (1 / log(min_mention_count + 1))

    Args:
        seed_page_ids: page_ids of the initial search results
        seed_scores: {page_id: score} from Zvec search
        top_k: max bridge results to return
        decay: score decay factor (default 0.4)
        exclude_types: page types to exclude (e.g. {"github-star"})
    """
    if not seed_page_ids:
        return []

    conn = _get_conn()
    try:
        # Step 1: Get entities for seed chunks (with role and mention_count)
        placeholders = ",".join("?" * len(seed_page_ids))
        seed_entities = conn.execute(f"""
            SELECT c.page_id, ce.entity_id, e.name, e.mention_count, ce.role
            FROM chunk_entities ce
            JOIN chunks c ON ce.chunk_id = c.id
            JOIN entities e ON ce.entity_id = e.id
            WHERE c.page_id IN ({placeholders})
        """, seed_page_ids).fetchall()

        if not seed_entities:
            return []

        # Build: entity_id → {name, mention_count, seed_roles: {page_id: role}}
        entity_info: dict[int, dict] = {}
        for row in seed_entities:
            eid = row["entity_id"]
            if eid not in entity_info:
                entity_info[eid] = {
                    "name": row["name"],
                    "mention_count": row["mention_count"],
                    "seed_roles": {},
                }
            entity_info[eid]["seed_roles"][row["page_id"]] = row["role"]

        entity_ids = list(entity_info.keys())

        # Step 2: Find candidate chunks sharing entities with seeds
        e_placeholders = ",".join("?" * len(entity_ids))
        candidates = conn.execute(f"""
            SELECT c.page_id, c.source_path, c.event_text,
                   ce.entity_id, ce.role as candidate_role
            FROM chunk_entities ce
            JOIN chunks c ON ce.chunk_id = c.id
            WHERE ce.entity_id IN ({e_placeholders})
              AND c.page_id NOT IN ({placeholders})
        """, entity_ids + seed_page_ids).fetchall()

        if not candidates:
            return []

        # Step 3: Group candidates by page_id, collect shared entities
        # candidate_page_id → {shared_entity_ids: [...], candidate_roles: {eid: role}}
        candidate_data: dict[str, dict] = {}
        for row in candidates:
            pid = row["page_id"]
            eid = row["entity_id"]
            if pid not in candidate_data:
                candidate_data[pid] = {
                    "source_path": row["source_path"],
                    "event_text": row["event_text"],
                    "shared_entity_ids": [],
                    "candidate_roles": {},
                }
            if eid not in candidate_data[pid]["shared_entity_ids"]:
                candidate_data[pid]["shared_entity_ids"].append(eid)
            candidate_data[pid]["candidate_roles"][eid] = row["candidate_role"]

        # Step 4: Apply three-layer filter
        max_seed_score = max(seed_scores.values()) if seed_scores else 1.0
        bridge_results = []

        for pid, data in candidate_data.items():
            shared_count = len(data["shared_entity_ids"])

            # Filter: shared ≥ 2 → pass
            if shared_count >= 2:
                pass
            # Filter: shared = 1 → check if both core
            elif shared_count == 1:
                eid = data["shared_entity_ids"][0]
                candidate_role = data["candidate_roles"].get(eid, "mention")
                # Check if ANY seed chunk has this entity as core
                any_seed_core = any(
                    role == "core"
                    for role in entity_info[eid]["seed_roles"].values()
                )
                if not (any_seed_core and candidate_role == "core"):
                    # Weak: skip
                    continue
            else:
                continue

            # Calculate score
            # mention_count: use the minimum (highest specificity) among shared entities
            min_mention = min(
                entity_info[eid]["mention_count"]
                for eid in data["shared_entity_ids"]
            )
            specificity = 1.0 / math.log(min_mention + 1) if min_mention > 0 else 1.0

            bridge_score = max_seed_score * decay * specificity

            # Check exclude_types
            source_path = data["source_path"]
            if exclude_types:
                # Read page type from source_path
                page_path = DATA_ROOT / source_path
                if page_path.exists():
                    page_content = read_file(page_path)
                    page_type = extract_frontmatter_field(page_content, "type") or "source"
                    if page_type in exclude_types:
                        continue

            bridge_results.append({
                "page_id": pid,
                "source_path": source_path,
                "event_text": data["event_text"],
                "score": round(bridge_score, 4),
                "shared_entity_count": shared_count,
                "shared_entities": [
                    entity_info[eid]["name"]
                    for eid in data["shared_entity_ids"]
                ],
                "backend": "entity_bridge",
            })

        # Sort by score descending
        bridge_results.sort(key=lambda x: x["score"], reverse=True)
        return bridge_results[:top_k]

    finally:
        conn.close()


# ── Rebuild ──────────────────────────────────────────────────────────


def rebuild_index() -> dict:
    """Rebuild the entire entity index from wiki pages.

    Scans concepts/ and summaries/ pages, extracts entities and event_text,
    infers roles, and populates the database.

    Returns stats: {chunks_indexed, entities_created, links_created, errors}
    """
    # Delete existing database and recreate
    if ENTITY_DB.exists():
        ENTITY_DB.unlink()

    init_db()

    stats = {"chunks_indexed": 0, "entities_created": 0, "links_created": 0, "errors": 0}

    # Collect all indexable pages
    pages = all_wiki_pages()

    # Build a set of existing concept page names for validation
    concept_names_on_disk = set()
    concepts_dir = WIKI_DIR / "concepts"
    if concepts_dir.exists():
        for p in concepts_dir.glob("*.md"):
            concept_names_on_disk.add(p.stem)

    for p in pages:
        try:
            pid = page_id(p)
            source_path = str(p.relative_to(DATA_ROOT)).replace("\\", "/")
            content = read_file(p)
            if not content.strip():
                continue

            # Determine page type
            page_type = extract_frontmatter_field(content, "type") or "source"
            is_concept = "concepts/" in pid

            # Extract event_text
            event_text = _extract_event_text(content)

            # Extract entity names
            if is_concept:
                # Concept page: entities = other concepts linked from this page
                entity_names = _extract_concept_names_from_concept(content)
                # The concept itself is always a core entity for this page
                self_name = p.stem
                if self_name not in entity_names:
                    entity_names.insert(0, self_name)
            else:
                # Summary/other page: entities = concepts from section 4
                entity_names = _extract_concept_names_from_summary(content)
                # Filter: only keep entities that exist as concept pages
                entity_names = [n for n in entity_names if n in concept_names_on_disk]

            if not entity_names and not event_text:
                continue

            # Infer roles
            if is_concept:
                # Concept page: self is always core, others are mention
                roles = {name: "core" if name == p.stem else "mention" for name in entity_names}
            else:
                roles = _infer_roles(content, entity_names)

            # Write to database
            upsert_chunk(pid, source_path, event_text)
            entity_ids = upsert_entities(entity_names)

            # Link chunk to entities
            conn = _get_conn()
            try:
                chunk_row = conn.execute(
                    "SELECT id FROM chunks WHERE page_id = ?", (pid,)
                ).fetchone()
                if chunk_row and entity_ids:
                    chunk_id = chunk_row["id"]
                    for name in entity_names:
                        if name in entity_ids:
                            role = roles.get(name, "mention")
                            conn.execute(
                                "INSERT INTO chunk_entities (chunk_id, entity_id, role) VALUES (?, ?, ?)",
                                (chunk_id, entity_ids[name], role),
                            )
                            stats["links_created"] += 1
                    conn.commit()
            finally:
                conn.close()

            stats["chunks_indexed"] += 1
            stats["entities_created"] += len(entity_ids)

        except Exception as e:
            print(f"  ⚠️ Error indexing {pid}: {e}", file=sys.stderr)
            stats["errors"] += 1

    return stats


# ── Consistency Check ────────────────────────────────────────────────


def check_consistency() -> list[str]:
    """Check entity index consistency against actual wiki files.

    Returns list of issue descriptions.
    """
    issues = []

    if not ENTITY_DB.exists():
        issues.append("Entity index database does not exist — run --rebuild first")
        return issues

    conn = _get_conn()
    try:
        # Check 1: Chunks pointing to non-existent files
        chunks = conn.execute("SELECT page_id, source_path FROM chunks").fetchall()
        for row in chunks:
            full_path = DATA_ROOT / row["source_path"]
            if not full_path.exists():
                issues.append(f"Stale chunk: {row['page_id']} → {row['source_path']} (file not found)")

        # Check 2: Wiki pages not in entity index
        all_pages = all_wiki_pages()
        indexed_page_ids = {row["page_id"] for row in chunks}
        for p in all_pages:
            pid = page_id(p)
            # 只检查总结页与概念页（不含 github-stars）
            if "summaries/" in pid or "concepts/" in pid:
                if pid not in indexed_page_ids:
                    issues.append(f"Missing from index: {pid}")

        # Check 3: Entities with mention_count = 0 (should have been deleted)
        zero_count = conn.execute(
            "SELECT name FROM entities WHERE mention_count <= 0"
        ).fetchall()
        for row in zero_count:
            issues.append(f"Entity with mention_count ≤ 0: {row['name']}")

        # Check 4: Orphan entities (no chunk links)
        orphan_entities = conn.execute("""
            SELECT e.name FROM entities e
            LEFT JOIN chunk_entities ce ON e.id = ce.entity_id
            WHERE ce.chunk_id IS NULL
        """).fetchall()
        for row in orphan_entities:
            issues.append(f"Orphan entity (no chunk links): {row['name']}")

    finally:
        conn.close()

    return issues


# ── Stats ────────────────────────────────────────────────────────────


def get_stats() -> dict:
    """Return entity index statistics."""
    if not ENTITY_DB.exists():
        return {"exists": False}

    conn = _get_conn()
    try:
        chunk_count = conn.execute("SELECT COUNT(*) as c FROM chunks").fetchone()["c"]
        entity_count = conn.execute("SELECT COUNT(*) as c FROM entities").fetchone()["c"]
        link_count = conn.execute("SELECT COUNT(*) as c FROM chunk_entities").fetchone()["c"]

        # Top entities by mention_count
        top_entities = conn.execute(
            "SELECT name, mention_count FROM entities ORDER BY mention_count DESC LIMIT 10"
        ).fetchall()

        # Role distribution
        core_count = conn.execute(
            "SELECT COUNT(*) as c FROM chunk_entities WHERE role = 'core'"
        ).fetchone()["c"]
        mention_count = conn.execute(
            "SELECT COUNT(*) as c FROM chunk_entities WHERE role = 'mention'"
        ).fetchone()["c"]

        return {
            "exists": True,
            "chunk_count": chunk_count,
            "entity_count": entity_count,
            "link_count": link_count,
            "core_links": core_count,
            "mention_links": mention_count,
            "top_entities": [
                {"name": r["name"], "mention_count": r["mention_count"]}
                for r in top_entities
            ],
        }
    finally:
        conn.close()


# ── Main ─────────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Entity Index for LLM Wiki: chunk-entity relationships for multi-hop retrieval")
    parser.add_argument("--rebuild", action="store_true",
                        help="Rebuild entire index from wiki pages")
    parser.add_argument("--stats", action="store_true",
                        help="Show index statistics")
    parser.add_argument("--check", action="store_true",
                        help="Run consistency check")
    parser.add_argument("--bridge", type=str, default=None,
                        help="Test bridge expansion for a page name")
    parser.add_argument("--file", type=str, default=None,
                        help="Index a single file by path")
    parser.add_argument("--top", type=int, default=5,
                        help="Top-K results for bridge test")
    args = parser.parse_args()

    if args.rebuild:
        print("Rebuilding entity index from wiki pages...")
        stats = rebuild_index()
        # 从库里读真实条数：stats['entities_created'] 累加的是"实体引用次数"（≈links），
        # 不等于 distinct 实体数，直接打印会误导（2026-09-16 修正措辞）
        conn = sqlite3.connect(str(ENTITY_DB))
        n_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        n_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        n_links = conn.execute("SELECT COUNT(*) FROM chunk_entities").fetchone()[0]
        conn.close()
        print(f"✅ Rebuild complete: {n_chunks} chunks, "
              f"{n_entities} entities, "
              f"{n_links} links, "
              f"{stats['errors']} errors")

    elif args.file:
        # Index a single file
        p = Path(args.file)
        if not p.exists():
            # Try relative to REPO_ROOT
            p = DATA_ROOT / args.file
        if not p.exists():
            print(f"File not found: {args.file}", file=sys.stderr)
            sys.exit(1)

        # Resolve to absolute path
        p = p.resolve()
        pid = page_id(p)
        source_path = str(p.relative_to(DATA_ROOT)).replace("\\", "/")
        content = read_file(p)
        is_concept = "concepts/" in pid

        # Build concept name set
        concept_names_on_disk = set()
        if CONCEPTS_DIR.exists():
            for cp in CONCEPTS_DIR.glob("*.md"):
                concept_names_on_disk.add(cp.stem)

        event_text = _extract_event_text(content)
        if is_concept:
            entity_names = _extract_concept_names_from_concept(content)
            self_name = p.stem
            if self_name not in entity_names:
                entity_names.insert(0, self_name)
            roles = {name: "core" if name == p.stem else "mention" for name in entity_names}
        else:
            entity_names = _extract_concept_names_from_summary(content)
            entity_names = [n for n in entity_names if n in concept_names_on_disk]
            roles = _infer_roles(content, entity_names)

        index_page(pid, source_path, event_text, entity_names, roles)
        print(f"✅ Indexed: {pid} (event: {event_text[:60]}..., entities: {len(entity_names)})")

    elif args.stats:
        stats = get_stats()
        if not stats.get("exists"):
            print("Entity index does not exist. Run --rebuild first.")
            return

        print("=== Entity Index Statistics ===")
        print(f"  Chunks:  {stats['chunk_count']}")
        print(f"  Entities: {stats['entity_count']}")
        print(f"  Links:   {stats['link_count']} (core: {stats['core_links']}, mention: {stats['mention_links']})")
        print()
        print("  Top entities by mention count:")
        for e in stats.get("top_entities", []):
            print(f"    {e['name']}: {e['mention_count']} mentions")

    elif args.check:
        issues = check_consistency()
        if not issues:
            print("✅ Entity index is consistent — no issues found")
        else:
            print(f"⚠️ Found {len(issues)} issue(s):")
            for issue in issues:
                print(f"  - {issue}")

    elif args.bridge:
        # Test bridge: find the page, then expand
        if not ENTITY_DB.exists():
            print("Entity index does not exist. Run --rebuild first.")
            return

        # Find the page
        query = args.bridge
        pages = all_wiki_pages()
        target = None
        for p in pages:
            if query in page_name(p) or query in page_id(p):
                target = p
                break

        if not target:
            print(f"Page not found: {query}")
            return

        pid = page_id(target)
        print(f"Testing bridge from: {pid}")

        results = bridge_by_entities(
            seed_page_ids=[pid],
            seed_scores={pid: 1.0},
            top_k=args.top,
        )

        if not results:
            print("No bridge results found")
        else:
            print(f"Found {len(results)} bridge result(s):")
            for r in results:
                print(f"  [{r['score']:.4f}] {r['page_id']}")
                print(f"    Shared entities ({r['shared_entity_count']}): {', '.join(r['shared_entities'])}")
                print(f"    Event: {r['event_text'][:80]}...")

    else:
        parser.print_help()


if __name__ == "__main__":
    # Windows 管道/控制台默认 GBK：emoji 会让 print 崩掉（工作其实已完成）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    main()
