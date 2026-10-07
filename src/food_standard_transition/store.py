"""食品标准迁移判定引擎的 SQLite 存储。

所有写操作在事务中完成；结论（decisions）、冲突（conflicts）、漂移（drifts）
为只追加表，任何更正都以新版本或解决记录体现，不做原地覆盖。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .domain import (
    Amendment,
    Batch,
    Clause,
    ConflictRecord,
    Decision,
    Drift,
    Exemption,
    FormulaSnapshot,
    LabelDeclaration,
    Method,
    MethodEquivalence,
    Record,
    Replacement,
    StandardPackage,
    TestResult,
    TransitionPolicy,
)


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str) -> object:
    return json.loads(value) if value else []


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._create_tables()
        self.connection.commit()

    def _create_tables(self) -> None:
        c = self.connection
        # 兼容既有基线
        c.execute("""
            CREATE TABLE IF NOT EXISTS records (
                record_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                state TEXT NOT NULL,
                revision INTEGER NOT NULL CHECK(revision > 0),
                created_at TEXT NOT NULL
            )
        """)

        c.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                package_id TEXT NOT NULL,
                version TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                repeal_date TEXT NOT NULL DEFAULT '',
                fingerprint TEXT NOT NULL UNIQUE,
                imported_at TEXT NOT NULL,
                categories TEXT NOT NULL DEFAULT '[]',
                PRIMARY KEY (package_id, version)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS clauses (
                clause_id TEXT PRIMARY KEY,
                package_id TEXT NOT NULL,
                version TEXT NOT NULL,
                clause_no TEXT NOT NULL,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                categories TEXT NOT NULL DEFAULT '[]',
                indicator TEXT NOT NULL DEFAULT '',
                basis TEXT NOT NULL DEFAULT 'per_100g',
                min_value REAL,
                max_value REAL,
                unit TEXT NOT NULL DEFAULT '',
                method_ref TEXT NOT NULL DEFAULT '',
                label_code TEXT NOT NULL DEFAULT '',
                label_required INTEGER NOT NULL DEFAULT 1,
                note TEXT NOT NULL DEFAULT '',
                origin TEXT NOT NULL DEFAULT 'base',
                amendment_id TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS amendments (
                amendment_id TEXT PRIMARY KEY,
                package_id TEXT NOT NULL,
                version TEXT NOT NULL,
                number TEXT NOT NULL,
                effective_date TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS replacements (
                amendment_id TEXT NOT NULL,
                clause_no TEXT NOT NULL,
                action TEXT NOT NULL,
                clause_id TEXT NOT NULL DEFAULT '',
                position INTEGER NOT NULL,
                PRIMARY KEY (amendment_id, clause_no)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS methods (
                method_id TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                version TEXT NOT NULL,
                indicator TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                issued_date TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS method_equivalences (
                method_id TEXT NOT NULL,
                other_method_id TEXT NOT NULL,
                indicator TEXT NOT NULL,
                comparable INTEGER NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (method_id, other_method_id, indicator)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS policies (
                policy_id TEXT PRIMARY KEY,
                category TEXT NOT NULL UNIQUE,
                old_package_id TEXT NOT NULL,
                old_version TEXT NOT NULL,
                new_package_id TEXT NOT NULL,
                new_version TEXT NOT NULL,
                new_effective_date TEXT NOT NULL,
                enforcement_date TEXT NOT NULL,
                old_label_use_until TEXT NOT NULL,
                stock_new_limits_from TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                lot_id TEXT PRIMARY KEY,
                product_name TEXT NOT NULL,
                category TEXT NOT NULL,
                factory_id TEXT NOT NULL,
                production_date TEXT NOT NULL,
                snapshot_id TEXT NOT NULL DEFAULT '',
                flow_state TEXT NOT NULL DEFAULT 'open',
                decided_at TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                snapshot_id TEXT PRIMARY KEY,
                lot_id TEXT NOT NULL,
                energy_kcal_per_100g REAL NOT NULL,
                components TEXT NOT NULL DEFAULT '[]',
                recorded_at TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS labels (
                lot_id TEXT NOT NULL,
                label_code TEXT NOT NULL,
                present INTEGER NOT NULL,
                label_version_date TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (lot_id, label_code)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS tests (
                test_id TEXT PRIMARY KEY,
                lot_id TEXT NOT NULL,
                indicator TEXT NOT NULL,
                value REAL NOT NULL,
                unit TEXT NOT NULL,
                method_id TEXT NOT NULL,
                tested_at TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS exemptions (
                exemption_id TEXT PRIMARY KEY,
                scope TEXT NOT NULL,
                scope_value TEXT NOT NULL,
                clause_id TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                drafted_by TEXT NOT NULL,
                approved_by TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL DEFAULT '',
                decided_at TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY,
                lot_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                result TEXT NOT NULL,
                production_date TEXT NOT NULL,
                target_date TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                evidence TEXT NOT NULL DEFAULT '{}',
                decided_at TEXT NOT NULL,
                review_of TEXT NOT NULL DEFAULT '',
                superseded_by TEXT NOT NULL DEFAULT '',
                UNIQUE (lot_id, version)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS conflicts (
                conflict_id TEXT PRIMARY KEY,
                lot_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                detail TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_by TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS drifts (
                drift_id TEXT PRIMARY KEY,
                package_id TEXT NOT NULL,
                version TEXT NOT NULL,
                existing_fingerprint TEXT NOT NULL,
                incoming_fingerprint TEXT NOT NULL,
                detected_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'locked',
                resolution TEXT NOT NULL DEFAULT ''
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS migration_items (
                campaign_id TEXT NOT NULL,
                lot_id TEXT NOT NULL,
                status TEXT NOT NULL,
                decision_id TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (campaign_id, lot_id)
            )
        """)

    # -- 基线记录 ----------------------------------------------------------

    def add(self, record: Record) -> Record:
        value = record.stamped()
        with self.connection:
            self.connection.execute(
                "INSERT INTO records(record_id, owner_id, state, revision, created_at) VALUES(?,?,?,?,?)",
                (value.record_id, value.owner_id, value.state, value.revision, value.created_at),
            )
        return value

    def get(self, record_id: str) -> Record | None:
        row = self.connection.execute(
            "SELECT record_id, owner_id, state, revision, created_at FROM records WHERE record_id=?",
            (record_id,),
        ).fetchone()
        return Record(**dict(row)) if row else None

    # -- 规则包 ------------------------------------------------------------

    def add_package(self, package: StandardPackage) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO packages(package_id, version, title, summary, effective_date,
                   repeal_date, fingerprint, imported_at, categories)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (package.package_id, package.version, package.title, package.summary,
                 package.effective_date, package.repeal_date, package.fingerprint,
                 package.imported_at, _dumps(list(package.categories))),
            )

    def package_fingerprint(self, package_id: str, version: str) -> str:
        row = self.connection.execute(
            "SELECT fingerprint FROM packages WHERE package_id=? AND version=?",
            (package_id, version),
        ).fetchone()
        return row["fingerprint"] if row else ""

    def package_version_exists(self, package_id: str, version: str) -> bool:
        return self.package_fingerprint(package_id, version) != ""

    def fingerprint_exists(self, fingerprint: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM packages WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        return row is not None

    def list_packages(self) -> list[StandardPackage]:
        rows = self.connection.execute(
            "SELECT * FROM packages ORDER BY package_id, version"
        ).fetchall()
        return [
            StandardPackage(
                package_id=r["package_id"], version=r["version"], title=r["title"],
                summary=r["summary"], effective_date=r["effective_date"],
                fingerprint=r["fingerprint"], imported_at=r["imported_at"],
                repeal_date=r["repeal_date"], categories=tuple(_loads(r["categories"])),
            )
            for r in rows
        ]

    # -- 条款与修改单 ------------------------------------------------------

    def add_clause(self, clause: Clause, origin: str = "base", amendment_id: str = "") -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO clauses(clause_id, package_id, version, clause_no, title, kind,
                   categories, indicator, basis, min_value, max_value, unit, method_ref,
                   label_code, label_required, note, origin, amendment_id)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (clause.clause_id, clause.package_id, clause.version, clause.clause_no,
                 clause.title, clause.kind, _dumps(list(clause.categories)), clause.indicator,
                 clause.basis, clause.min_value, clause.max_value, clause.unit,
                 clause.method_ref, clause.label_code, 1 if clause.label_required else 0,
                 clause.note, origin, amendment_id),
            )

    def clause_exists(self, clause_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM clauses WHERE clause_id=?", (clause_id,)
        ).fetchone() is not None

    def list_base_clauses(self) -> list[Clause]:
        rows = self.connection.execute(
            "SELECT * FROM clauses WHERE origin='base' ORDER BY package_id, version, clause_no"
        ).fetchall()
        return [self._row_to_clause(r) for r in rows]

    def get_clause_row(self, clause_id: str) -> Clause | None:
        row = self.connection.execute("SELECT * FROM clauses WHERE clause_id=?", (clause_id,)).fetchone()
        return self._row_to_clause(row) if row else None

    @staticmethod
    def _row_to_clause(r: sqlite3.Row) -> Clause:
        return Clause(
            clause_id=r["clause_id"], package_id=r["package_id"], version=r["version"],
            clause_no=r["clause_no"], title=r["title"], kind=r["kind"],
            categories=tuple(_loads(r["categories"])), indicator=r["indicator"],
            basis=r["basis"], min_value=r["min_value"], max_value=r["max_value"],
            unit=r["unit"], method_ref=r["method_ref"], label_code=r["label_code"],
            label_required=bool(r["label_required"]), note=r["note"],
        )

    def add_amendment(self, amendment: Amendment) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO amendments(amendment_id, package_id, version, number,
                   effective_date, summary) VALUES(?,?,?,?,?,?)""",
                (amendment.amendment_id, amendment.package_id, amendment.version,
                 amendment.number, amendment.effective_date, amendment.summary),
            )
            for position, replacement in enumerate(amendment.replacements):
                self.connection.execute(
                    "INSERT INTO replacements(amendment_id, clause_no, action, clause_id, position)"
                    " VALUES(?,?,?,?,?)",
                    (amendment.amendment_id, replacement.clause_no, replacement.action,
                     replacement.clause.clause_id if replacement.clause else "", position),
                )

    def amendment_exists(self, amendment_id: str) -> bool:
        return self.connection.execute(
            "SELECT 1 FROM amendments WHERE amendment_id=?", (amendment_id,)
        ).fetchone() is not None

    def list_amendments(self) -> list[Amendment]:
        rows = self.connection.execute(
            "SELECT * FROM amendments ORDER BY effective_date, number"
        ).fetchall()
        result: list[Amendment] = []
        for r in rows:
            rep_rows = self.connection.execute(
                "SELECT * FROM replacements WHERE amendment_id=? ORDER BY position",
                (r["amendment_id"],),
            ).fetchall()
            replacements: list[Replacement] = []
            for rr in rep_rows:
                clause = self.get_clause_row(rr["clause_id"]) if rr["clause_id"] else None
                replacements.append(Replacement(clause_no=rr["clause_no"], action=rr["action"], clause=clause))
            result.append(Amendment(
                amendment_id=r["amendment_id"], package_id=r["package_id"],
                version=r["version"], number=r["number"],
                effective_date=r["effective_date"], summary=r["summary"],
                replacements=tuple(replacements),
            ))
        return result

    # -- 方法 --------------------------------------------------------------

    def add_method(self, method: Method) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO methods(method_id, code, version, indicator,
                   title, issued_date) VALUES(?,?,?,?,?,?)""",
                (method.method_id, method.code, method.version, method.indicator,
                 method.title, method.issued_date),
            )

    def list_methods(self) -> list[Method]:
        rows = self.connection.execute("SELECT * FROM methods ORDER BY method_id").fetchall()
        return [Method(**dict(r)) for r in rows]

    def add_equivalence(self, equivalence: MethodEquivalence) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO method_equivalences(method_id, other_method_id,
                   indicator, comparable, note) VALUES(?,?,?,?,?)""",
                (equivalence.method_id, equivalence.other_method_id, equivalence.indicator,
                 1 if equivalence.comparable else 0, equivalence.note),
            )

    def list_equivalences(self) -> list[MethodEquivalence]:
        rows = self.connection.execute(
            "SELECT * FROM method_equivalences ORDER BY method_id, other_method_id"
        ).fetchall()
        return [
            MethodEquivalence(
                method_id=r["method_id"], other_method_id=r["other_method_id"],
                indicator=r["indicator"], comparable=bool(r["comparable"]), note=r["note"],
            )
            for r in rows
        ]

    # -- 过渡政策 ----------------------------------------------------------

    def add_policy(self, policy: TransitionPolicy) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO policies(policy_id, category, old_package_id, old_version,
                   new_package_id, new_version, new_effective_date, enforcement_date,
                   old_label_use_until, stock_new_limits_from, note)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (policy.policy_id, policy.category, policy.old_package_id, policy.old_version,
                 policy.new_package_id, policy.new_version, policy.new_effective_date,
                 policy.enforcement_date, policy.old_label_use_until,
                 policy.stock_new_limits_from, policy.note),
            )

    def list_policies(self) -> list[TransitionPolicy]:
        rows = self.connection.execute("SELECT * FROM policies ORDER BY category").fetchall()
        return [TransitionPolicy(**dict(r)) for r in rows]

    def policy_for_category(self, category: str) -> TransitionPolicy | None:
        row = self.connection.execute("SELECT * FROM policies WHERE category=?", (category,)).fetchone()
        return TransitionPolicy(**dict(row)) if row else None

    # -- 批次 / 快照 / 标签 / 检测 -----------------------------------------

    def upsert_batch(self, batch: Batch) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO batches(lot_id, product_name, category, factory_id,
                   production_date, snapshot_id, flow_state, decided_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(lot_id) DO UPDATE SET
                     product_name=excluded.product_name,
                     category=excluded.category,
                     factory_id=excluded.factory_id,
                     production_date=excluded.production_date,
                     snapshot_id=excluded.snapshot_id
                   WHERE batches.flow_state='open'""",
                (batch.lot_id, batch.product_name, batch.category, batch.factory_id,
                 batch.production_date, batch.snapshot_id, batch.flow_state, batch.decided_at),
            )

    def get_batch(self, lot_id: str) -> Batch | None:
        row = self.connection.execute("SELECT * FROM batches WHERE lot_id=?", (lot_id,)).fetchone()
        return Batch(**dict(row)) if row else None

    def list_lots(self) -> list[str]:
        return [r["lot_id"] for r in self.connection.execute("SELECT lot_id FROM batches ORDER BY lot_id")]

    def mark_batch_decided(self, lot_id: str, decided_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE batches SET flow_state='decided', decided_at=? WHERE lot_id=?",
                (decided_at, lot_id),
            )

    def add_snapshot(self, snapshot: FormulaSnapshot) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT OR REPLACE INTO snapshots(snapshot_id, lot_id, energy_kcal_per_100g,
                   components, recorded_at) VALUES(?,?,?,?,?)""",
                (snapshot.snapshot_id, snapshot.lot_id, snapshot.energy_kcal_per_100g,
                 _dumps([list(pair) for pair in snapshot.components]), snapshot.recorded_at),
            )
            self.connection.execute(
                "UPDATE batches SET snapshot_id=? WHERE lot_id=?",
                (snapshot.snapshot_id, snapshot.lot_id),
            )

    def get_snapshot(self, snapshot_id: str) -> FormulaSnapshot | None:
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if not row:
            return None
        return FormulaSnapshot(
            snapshot_id=row["snapshot_id"], lot_id=row["lot_id"],
            energy_kcal_per_100g=row["energy_kcal_per_100g"],
            components=tuple((pair[0], pair[1]) for pair in _loads(row["components"])),
            recorded_at=row["recorded_at"],
        )

    def snapshot_for_lot(self, lot_id: str) -> FormulaSnapshot | None:
        row = self.connection.execute(
            "SELECT snapshot_id FROM batches WHERE lot_id=?", (lot_id,)
        ).fetchone()
        if not row or not row["snapshot_id"]:
            return None
        return self.get_snapshot(row["snapshot_id"])

    def upsert_label(self, declaration: LabelDeclaration) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO labels(lot_id, label_code, present, label_version_date)
                   VALUES(?,?,?,?)
                   ON CONFLICT(lot_id, label_code) DO UPDATE SET
                     present=excluded.present,
                     label_version_date=excluded.label_version_date""",
                (declaration.lot_id, declaration.label_code,
                 1 if declaration.present else 0, declaration.label_version_date),
            )

    def labels_for_lot(self, lot_id: str) -> list[LabelDeclaration]:
        rows = self.connection.execute("SELECT * FROM labels WHERE lot_id=?", (lot_id,)).fetchall()
        return [
            LabelDeclaration(
                lot_id=r["lot_id"], label_code=r["label_code"],
                present=bool(r["present"]), label_version_date=r["label_version_date"],
            )
            for r in rows
        ]

    def add_test(self, test: TestResult) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO tests(test_id, lot_id, indicator, value, unit, method_id,
                   tested_at, received_at) VALUES(?,?,?,?,?,?,?,?)""",
                (test.test_id, test.lot_id, test.indicator, test.value, test.unit,
                 test.method_id, test.tested_at, test.received_at),
            )

    def tests_for_lot(self, lot_id: str) -> list[TestResult]:
        rows = self.connection.execute(
            "SELECT * FROM tests WHERE lot_id=? ORDER BY tested_at", (lot_id,)
        ).fetchall()
        return [TestResult(**dict(r)) for r in rows]

    # -- 豁免 --------------------------------------------------------------

    def add_exemption(self, exemption: Exemption) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO exemptions(exemption_id, scope, scope_value, clause_id, reason,
                   drafted_by, approved_by, status, created_at, decided_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (exemption.exemption_id, exemption.scope, exemption.scope_value,
                 exemption.clause_id, exemption.reason, exemption.drafted_by,
                 exemption.approved_by, exemption.status, exemption.created_at,
                 exemption.decided_at),
            )

    def get_exemption(self, exemption_id: str) -> Exemption | None:
        row = self.connection.execute(
            "SELECT * FROM exemptions WHERE exemption_id=?", (exemption_id,)
        ).fetchone()
        return Exemption(**dict(row)) if row else None

    def update_exemption_status(self, exemption_id: str, status: str, approver: str, decided_at: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE exemptions SET status=?, approved_by=?, decided_at=? WHERE exemption_id=?",
                (status, approver, decided_at, exemption_id),
            )

    def exemptions_for(self, lot_id: str, category: str) -> list[Exemption]:
        rows = self.connection.execute(
            "SELECT * FROM exemptions WHERE status='approved' AND "
            "((scope='lot' AND scope_value=?) OR (scope='category' AND scope_value=?))",
            (lot_id, category),
        ).fetchall()
        return [Exemption(**dict(r)) for r in rows]

    # -- 决定（只追加） ----------------------------------------------------

    def add_decision(self, decision: Decision) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO decisions(decision_id, lot_id, version, result, production_date,
                   target_date, summary, evidence, decided_at, review_of, superseded_by)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (decision.decision_id, decision.lot_id, decision.version, decision.result,
                 decision.production_date, decision.target_date, decision.summary,
                 decision.evidence, decision.decided_at, decision.review_of,
                 decision.superseded_by),
            )
            if decision.review_of:
                self.connection.execute(
                    "UPDATE decisions SET superseded_by=? WHERE decision_id=?",
                    (decision.decision_id, decision.review_of),
                )

    def next_decision_version(self, lot_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM decisions WHERE lot_id=?", (lot_id,)
        ).fetchone()
        return int(row["v"]) + 1

    def latest_decision(self, lot_id: str) -> Decision | None:
        row = self.connection.execute(
            "SELECT * FROM decisions WHERE lot_id=? ORDER BY version DESC LIMIT 1", (lot_id,)
        ).fetchone()
        return self._row_to_decision(row) if row else None

    def get_decision(self, decision_id: str) -> Decision | None:
        row = self.connection.execute(
            "SELECT * FROM decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        return self._row_to_decision(row) if row else None

    def list_decisions(self, lot_id: str) -> list[Decision]:
        rows = self.connection.execute(
            "SELECT * FROM decisions WHERE lot_id=? ORDER BY version", (lot_id,)
        ).fetchall()
        return [self._row_to_decision(r) for r in rows]

    @staticmethod
    def _row_to_decision(r: sqlite3.Row) -> Decision:
        return Decision(
            decision_id=r["decision_id"], lot_id=r["lot_id"], version=r["version"],
            result=r["result"], production_date=r["production_date"],
            target_date=r["target_date"], summary=r["summary"], evidence=r["evidence"],
            decided_at=r["decided_at"], review_of=r["review_of"], superseded_by=r["superseded_by"],
        )

    # -- 冲突（只追加） ----------------------------------------------------

    def add_conflict(self, conflict: ConflictRecord) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO conflicts(conflict_id, lot_id, kind, detail, created_at, resolved_by)
                   VALUES(?,?,?,?,?,?)""",
                (conflict.conflict_id, conflict.lot_id, conflict.kind, conflict.detail,
                 conflict.created_at, conflict.resolved_by),
            )

    def open_conflicts(self, lot_id: str) -> list[ConflictRecord]:
        rows = self.connection.execute(
            "SELECT * FROM conflicts WHERE lot_id=? AND resolved_by='' ORDER BY created_at",
            (lot_id,),
        ).fetchall()
        return [ConflictRecord(**dict(r)) for r in rows]

    def resolve_conflicts(self, lot_id: str, decision_id: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE conflicts SET resolved_by=? WHERE lot_id=? AND resolved_by=''",
                (decision_id, lot_id),
            )

    def list_conflicts(self) -> list[ConflictRecord]:
        rows = self.connection.execute(
            "SELECT * FROM conflicts ORDER BY created_at"
        ).fetchall()
        return [ConflictRecord(**dict(r)) for r in rows]

    # -- 漂移 --------------------------------------------------------------

    def add_drift(self, drift: Drift) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO drifts(drift_id, package_id, version, existing_fingerprint,
                   incoming_fingerprint, detected_at, status, resolution)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (drift.drift_id, drift.package_id, drift.version, drift.existing_fingerprint,
                 drift.incoming_fingerprint, drift.detected_at, drift.status, drift.resolution),
            )

    def drift_exists_open(self, package_id: str, version: str, incoming_fingerprint: str) -> bool:
        return self.find_open_drift(package_id, version, incoming_fingerprint) is not None

    def find_open_drift(self, package_id: str, version: str, incoming_fingerprint: str) -> Drift | None:
        row = self.connection.execute(
            """SELECT * FROM drifts WHERE package_id=? AND version=?
               AND incoming_fingerprint=? AND status='locked'""",
            (package_id, version, incoming_fingerprint),
        ).fetchone()
        return Drift(**dict(row)) if row else None

    def list_drifts(self, status: str | None = None) -> list[Drift]:
        if status:
            rows = self.connection.execute(
                "SELECT * FROM drifts WHERE status=? ORDER BY detected_at", (status,)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM drifts ORDER BY detected_at").fetchall()
        return [Drift(**dict(r)) for r in rows]

    def resolve_drift(self, drift_id: str, resolution: str) -> None:
        with self.connection:
            self.connection.execute(
                "UPDATE drifts SET status='resolved', resolution=? WHERE drift_id=?",
                (resolution, drift_id),
            )

    # -- 批量迁移检查点 ----------------------------------------------------

    def get_migration_item(self, campaign_id: str, lot_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM migration_items WHERE campaign_id=? AND lot_id=?",
            (campaign_id, lot_id),
        ).fetchone()

    def upsert_migration_item(self, campaign_id: str, lot_id: str, status: str,
                              decision_id: str, updated_at: str) -> None:
        with self.connection:
            self.connection.execute(
                """INSERT INTO migration_items(campaign_id, lot_id, status, decision_id, updated_at)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(campaign_id, lot_id) DO UPDATE SET
                     status=excluded.status,
                     decision_id=excluded.decision_id,
                     updated_at=excluded.updated_at""",
                (campaign_id, lot_id, status, decision_id, updated_at),
            )

    def migration_pending(self, campaign_id: str, lot_ids: list[str]) -> list[str]:
        """检查点：已完成（done）的批次不再返回；conflict 仍可重试。"""
        if not lot_ids:
            return []
        placeholders = ",".join("?" for _ in lot_ids)
        rows = self.connection.execute(
            f"""SELECT lot_id FROM migration_items
                WHERE campaign_id=? AND status='done' AND lot_id IN ({placeholders})""",
            [campaign_id, *lot_ids],
        ).fetchall()
        done = {r["lot_id"] for r in rows}
        return [lot_id for lot_id in lot_ids if lot_id not in done]

    def list_migration(self, campaign_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM migration_items WHERE campaign_id=? ORDER BY lot_id",
            (campaign_id,),
        ).fetchall()
