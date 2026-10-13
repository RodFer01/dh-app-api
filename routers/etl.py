"""ETL management router — upload files and trigger ETL jobs."""
import sys
import os
import shutil
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, Depends, UploadFile, File, HTTPException, BackgroundTasks
from pydantic import BaseModel
from auth import verify_token

router = APIRouter(prefix="/api/etl", tags=["etl"])

ETL_DIR = Path(__file__).parent.parent / "etl"

# ── In-memory job store ────────────────────────────────────────────────────────
_jobs: dict[str, dict] = {}
_lock = threading.Lock()

def _job_id():
    return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")


# ── DB persistence helpers ────────────────────────────────────────────────────

def _persist_job(job: dict):
    """Upsert job record to etl_job_history table (works on both HANA and PostgreSQL)."""
    try:
        from database import execute, IS_HANA
        finished = job.get("finished_at")
        rows = job.get("rows") or 0
        err  = (job.get("error") or "")[:2000]
        log  = (job.get("log") or "")[-4000:]

        if IS_HANA:
            execute("""
                MERGE INTO etl_job_history USING (SELECT ? AS k FROM DUMMY) src ON (job_id = src.k)
                WHEN MATCHED THEN UPDATE SET
                    status=?, finished_at=?, rows_affected=?, error_msg=?, log_text=?
                WHEN NOT MATCHED THEN INSERT
                    (job_id,source_id,source_label,status,started_at,finished_at,rows_affected,error_msg,log_text)
                    VALUES(?,?,?,?,?,?,?,?,?)
            """, [
                job["id"],
                job.get("status"), finished, rows, err, log,
                job["id"], job.get("source_id"), job.get("name"),
                job.get("status"), job.get("started_at"), finished, rows, err, log,
            ])
        else:
            execute("""
                INSERT INTO etl_job_history
                    (job_id,source_id,source_label,status,started_at,finished_at,rows_affected,error_msg,log_text)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (job_id) DO UPDATE SET
                    status=EXCLUDED.status, finished_at=EXCLUDED.finished_at,
                    rows_affected=EXCLUDED.rows_affected, error_msg=EXCLUDED.error_msg,
                    log_text=EXCLUDED.log_text
            """, [
                job["id"], job.get("source_id"), job.get("name"),
                job.get("status"), job.get("started_at"), finished, rows, err, log,
            ])
    except Exception:
        pass


def _load_history_from_db() -> list[dict]:
    """Load recent job history from DB (fallback when in-memory is empty)."""
    try:
        from database import query, IS_HANA
        if IS_HANA:
            rows = query("""
                SELECT TOP 50 job_id AS id, source_id, source_label AS name,
                       status,
                       TO_VARCHAR(started_at,'YYYY-MM-DD') || 'T' || TO_VARCHAR(started_at,'HH24:MI:SS') || 'Z' AS started_at,
                       TO_VARCHAR(finished_at,'YYYY-MM-DD') || 'T' || TO_VARCHAR(finished_at,'HH24:MI:SS') || 'Z' AS finished_at,
                       rows_affected AS rows, error_msg AS error, log_text AS log
                FROM etl_job_history
                ORDER BY started_at DESC
            """)
        else:
            rows = query("""
                SELECT job_id AS id, source_id, source_label AS name,
                       status, started_at::text AS started_at,
                       finished_at::text AS finished_at,
                       rows_affected AS rows, error_msg AS error, log_text AS log
                FROM etl_job_history
                ORDER BY started_at DESC
                LIMIT 50
            """)
        return [dict(r) for r in rows]
    except Exception:
        return []


# ── ETL catalog ───────────────────────────────────────────────────────────────
# Each entry defines what file key(s) to update and which ETL modules to run
SOURCES = [
    {
        "id":       "pipeline",
        "label":    "Pipeline (Current Week)",
        "desc":     "Global Cloud Pipeline Data Analyzer_2026.csv",
        "file_key": "pipeline",
        "accept":   ".csv",
        "etls":     ["etl_pipeline", "etl_pipeline_exploded"],
        "etl_kwargs": {"etl_pipeline": {"last_week": False}},
    },
    {
        "id":       "pipeline_lw",
        "label":    "Pipeline (Last Week)",
        "desc":     "Global Cloud Pipeline Data Analyzer_2026_last week.csv",
        "file_key": "pipeline_lw",
        "accept":   ".csv",
        "etls":     ["etl_pipeline"],
        "etl_kwargs": {"etl_pipeline": {"last_week": True}},
    },
    {
        "id":       "melody",
        "label":    "Melody Activities",
        "desc":     "CA Melody Activity Details Data Analyzer_2026.csv",
        "file_key": "melody",
        "accept":   ".csv",
        "etls":     ["etl_melody"],
    },
    {
        "id":       "presales",
        "label":    "Presales (Deal Management)",
        "desc":     "Deal Management Cloud Opportunity Details_2026.csv",
        "file_key": "presales",
        "accept":   ".csv",
        "etls":     ["etl_presales"],
    },
    {
        "id":       "vat_full",
        "label":    "VAT Full",
        "desc":     "Sales Team VAT Report Data Analyzer_2026_full.csv",
        "file_key": "full_vat",
        "accept":   ".csv",
        "etls":     ["etl_vat"],
        "etl_kwargs": {"etl_vat": {"only": "full_vat"}},
    },
    {
        "id":       "vat_dh",
        "label":    "VAT DH",
        "desc":     "Sales Team VAT Report Data Analyzer_DH_2026.csv",
        "file_key": "dh_vat",
        "accept":   ".csv",
        "etls":     ["etl_vat", "etl_pipeline_exploded"],
        "etl_kwargs": {"etl_vat": {"only": "dh_vat"}},
    },
    {
        "id":       "targets",
        "label":    "Targets",
        "desc":     "Targets.xlsx",
        "file_key": "targets",
        "accept":   ".xlsx",
        "etls":     ["etl_targets"],
    },
    {
        "id":       "dda",
        "label":    "DDA (DET.xlsx)",
        "desc":     "DET.xlsx — formato original",
        "file_key": "dda",
        "accept":   ".xlsx",
        "etls":     ["etl_dda"],
    },
    {
        "id":       "lac_cloud_erp",
        "label":    "LAC Cloud ERP",
        "desc":     "LAC Cloud ERP.xlsx — tab DET dd.mm.aa (DDA Status + Range Aging)",
        "file_key": "lac_cloud_erp",
        "accept":   ".xlsx",
        "etls":     ["etl_dda"],
    },
    {
        "id":       "rvp",
        "label":    "RVP / Rating",
        "desc":     "Master_RPV_Global_Rating_2026.xlsx",
        "file_key": "rvp",
        "accept":   ".xlsx",
        "etls":     ["etl_rvp"],
    },
    {
        "id":       "progression",
        "label":    "Progression Framework",
        "desc":     "Progression Framework_*.xlsx — tab Details (Prioritie, Probability %)",
        "file_key": "progression",
        "accept":   ".xlsx",
        "etls":     ["etl_progression"],
    },
    {
        "id":       "innovation_dash",
        "label":    "Innovation Dashboard",
        "desc":     "InnovationDash_*.xlsx — Base instalada por BP (sheet myListBP)",
        "file_key": "innovation_dash",
        "accept":   ".xlsx",
        "etls":     ["etl_innovation"],
    },
    {
        "id":       "mastercode",
        "label":    "Mastercode (SPEC/IAE)",
        "desc":     "ML*.xlsx — Business Partner → IAE, SPEC CX/HXM/BTP/etc.",
        "file_key": "mastercode",
        "accept":   ".xlsx",
        "etls":     ["etl_mastercode"],
    },
    # ── 2025 Historical ───────────────────────────────────────────────────────
    {
        "id":       "melody_2025",
        "label":    "Melody 2025 (Histórico)",
        "desc":     "CA Melody Activity Details Data Analyzer_2025.csv",
        "file_key": "melody_2025",
        "accept":   ".csv",
        "etls":     ["etl_melody_2025"],
    },
    {
        "id":       "pipeline_2025",
        "label":    "Pipeline 2025 (Histórico)",
        "desc":     "Global Cloud Pipeline Data Analyzer_2025.csv",
        "file_key": "pipeline_2025",
        "accept":   ".csv",
        "etls":     ["etl_pipeline_2025"],
    },
    {
        "id":       "vat_full_2025",
        "label":    "VAT Full 2025 (Histórico)",
        "desc":     "Sales Team VAT Report Data Analyzer_2025_full.csv",
        "file_key": "full_vat_2025",
        "accept":   ".csv",
        "etls":     ["etl_vat_2025"],
        "etl_kwargs": {"etl_vat_2025": {"only": "full_vat_2025"}},
    },
    {
        "id":       "vat_dh_2025",
        "label":    "VAT DH 2025 (Histórico)",
        "desc":     "Sales Team VAT Report Data Analyzer_DH_2025.csv",
        "file_key": "dh_vat_2025",
        "accept":   ".csv",
        "etls":     ["etl_vat_2025"],
        "etl_kwargs": {"etl_vat_2025": {"only": "dh_vat_2025"}},
    },
    {
        "id":       "outreach_contacts",
        "label":    "Lead Intelligence (Outreach / Databricks)",
        "desc":     "Busca contatos e lead scores do Databricks → OUTREACH_CONTACTS. Requer DATABRICKS_TOKEN válido.",
        "file_key": None,
        "accept":   None,
        "etls":     ["etl_outreach_contacts"],
    },
    {
        "id":       "full_run",
        "label":    "Full ETL (todos os módulos)",
        "desc":     "Executa todos os ETLs na ordem correta (run_all.py)",
        "file_key": None,
        "accept":   None,
        "etls":     ["run_all"],
    },
]


@router.get("/sources")
def list_sources(_user=Depends(verify_token)):
    """List available ETL sources."""
    return SOURCES


@router.get("/jobs")
def list_jobs(_user=Depends(verify_token)):
    """List all jobs — merges in-memory (current session) with DB history."""
    with _lock:
        mem_jobs = sorted(_jobs.values(), key=lambda j: j["started_at"], reverse=True)

    # If memory is empty (e.g. after restart), load from DB
    if not mem_jobs:
        return _load_history_from_db()

    # Merge: memory takes priority for current session, DB fills in older entries
    mem_ids = {j["id"] for j in mem_jobs}
    db_jobs  = [j for j in _load_history_from_db() if j["id"] not in mem_ids]
    combined = sorted(mem_jobs + db_jobs, key=lambda j: j.get("started_at",""), reverse=True)
    return combined[:50]


@router.get("/jobs/{job_id}")
def get_job(job_id: str, _user=Depends(verify_token)):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        # Try DB
        rows = _load_history_from_db()
        job = next((j for j in rows if j["id"] == job_id), None)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _run_etl_job(job_id: str, source_id: str, saved_paths: list[Path], file_key: Optional[str]):
    """Run in a background thread. Updates job state in _jobs."""
    log_lines = []

    def _log(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        log_lines.append(line)
        with _lock:
            if job_id in _jobs:
                _jobs[job_id]["log"] = "\n".join(log_lines)

    try:
        # Add ETL_DIR to path so imports work
        if str(ETL_DIR) not in sys.path:
            sys.path.insert(0, str(ETL_DIR))
        os.chdir(ETL_DIR)

        # Find the source definition
        src = next((s for s in SOURCES if s["id"] == source_id), None)
        if not src:
            raise ValueError(f"Source '{source_id}' not found")

        total_rows = 0
        multi_file_sources = {"pipeline", "pipeline_lw"}

        if source_id == "full_run":
            _log("Iniciando Full ETL …")
            import run_all as _run_all
            import importlib
            importlib.reload(_run_all)
            import etl_fx, etl_targets, etl_vat, etl_melody, etl_presales, etl_dda, etl_rvp, etl_progression, etl_pipeline, etl_pipeline_exploded
            for mod in [etl_fx, etl_targets, etl_vat, etl_melody, etl_presales, etl_dda, etl_rvp, etl_progression, etl_pipeline, etl_pipeline_exploded]:
                importlib.reload(mod)
            steps = [
                ("fx",                 etl_fx.run,                   {}),
                ("targets",            etl_targets.run,              {}),
                ("vat",                etl_vat.run,                  {}),
                ("melody",             etl_melody.run,               {}),
                ("presales",           etl_presales.run,             {}),
                ("dda",                etl_dda.run,                  {}),
                ("rvp",                etl_rvp.run,                  {}),
                ("progression",        etl_progression.run,          {}),
                ("pipeline",           etl_pipeline.run,             {"last_week": False}),
                ("pipeline_exploded",  etl_pipeline_exploded.run,    {}),
            ]
            for step_name, fn, kwargs in steps:
                _log(f"  ▶ {step_name} …")
                t0 = time.time()
                result = fn(**kwargs)
                elapsed = time.time() - t0
                _log(f"  ✓ {step_name}: {result}  ({elapsed:.1f}s)")
                if isinstance(result, int):
                    total_rows += result

        elif source_id in multi_file_sources and len(saved_paths) >= 1:
            # Multi-file incremental load — uses run_multi to avoid external merge
            import importlib, etl_pipeline
            importlib.reload(etl_pipeline)
            last_week = (source_id == "pipeline_lw")
            _log(f"Carregando {len(saved_paths)} arquivo(s) incrementalmente …")
            t0 = time.time()
            result = etl_pipeline.run_multi(saved_paths, last_week=last_week)
            elapsed = time.time() - t0
            _log(f"✓ run_multi: {result}  ({elapsed:.1f}s)")
            total_rows += sum(v for v in result.values() if isinstance(v, int))

            if source_id == "pipeline":
                import etl_pipeline_exploded
                importlib.reload(etl_pipeline_exploded)
                _log("Executando etl_pipeline_exploded …")
                t0 = time.time()
                n = etl_pipeline_exploded.run()
                elapsed = time.time() - t0
                _log(f"✓ etl_pipeline_exploded: {n}  ({elapsed:.1f}s)")

        else:
            # Single-file path: copy to config location then run as before
            if saved_paths and file_key:
                import config
                dest = config.FILES[file_key]
                saved_path = saved_paths[0]
                _log(f"Copiando arquivo para {dest.name} …")
                shutil.copy2(saved_path, dest)
                _log(f"Arquivo salvo ({saved_path.stat().st_size / 1024:.0f} KB)")
                saved_path.unlink(missing_ok=True)

            import importlib
            kwargs_map = src.get("etl_kwargs", {})
            for etl_name in src["etls"]:
                _log(f"Executando {etl_name} …")
                mod = importlib.import_module(etl_name)
                importlib.reload(mod)
                t0 = time.time()
                kwargs = kwargs_map.get(etl_name, {})
                result = mod.run(**kwargs)
                elapsed = time.time() - t0
                _log(f"✓ {etl_name}: {result}  ({elapsed:.1f}s)")
                if isinstance(result, int):
                    total_rows += result
                elif isinstance(result, dict):
                    total_rows += sum(v for v in result.values() if isinstance(v, int))

        # Clean up any remaining temp files
        for p in saved_paths:
            try:
                if p.exists():
                    p.unlink()
            except Exception:
                pass

        _log(f"\n✅ Concluído — {total_rows} linhas processadas")
        with _lock:
            _jobs[job_id].update({
                "status": "success",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "rows": total_rows,
                "log": "\n".join(log_lines),
            })
            _persist_job(_jobs[job_id])

    except Exception as e:
        err = traceback.format_exc()
        _log(f"\n❌ Erro: {e}\n{err}")
        with _lock:
            _jobs[job_id].update({
                "status": "error",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error": str(e),
                "log": "\n".join(log_lines),
            })
            _persist_job(_jobs[job_id])


@router.post("/run/{source_id}")
async def run_etl(
    source_id: str,
    background_tasks: BackgroundTasks,
    files: List[UploadFile] = File(default=[]),
    _user=Depends(verify_token),
):
    """
    Upload one or more files and trigger the ETL for source_id.
    For pipeline sources, multiple files are loaded incrementally (no external merge needed).
    Returns a job_id immediately; poll /api/etl/jobs/{job_id} for status.
    """
    src = next((s for s in SOURCES if s["id"] == source_id), None)
    if not src:
        raise HTTPException(status_code=404, detail=f"Source '{source_id}' not found")

    file_key = src.get("file_key")
    saved_paths: List[Path] = []

    valid_files = [f for f in files if f and f.filename]
    if valid_files:
        for f in valid_files:
            tmp = ETL_DIR / f"_upload_{source_id}_{_job_id()}{Path(f.filename).suffix}"
            contents = await f.read()
            tmp.write_bytes(contents)
            saved_paths.append(tmp)
    elif file_key and source_id != "full_run":
        # No file uploaded — use the existing file on disk
        if str(ETL_DIR) not in sys.path:
            sys.path.insert(0, str(ETL_DIR))
        import config
        existing = config.FILES.get(file_key)
        if not existing or not Path(existing).exists():
            raise HTTPException(
                status_code=400,
                detail=f"Nenhum arquivo enviado e o arquivo de origem não existe: {existing}"
            )

    job_id = _job_id()
    file_count = len(saved_paths)
    label = src["label"] + (f" ({file_count} arquivo(s))" if file_count > 1 else "")
    with _lock:
        _jobs[job_id] = {
            "id": job_id,
            "source_id": source_id,
            "name": label,
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "rows": 0,
            "error": None,
            "log": f"[{datetime.now().strftime('%H:%M:%S')}] Iniciando {label} …\n",
        }
        _persist_job(_jobs[job_id])

    background_tasks.add_task(_run_etl_job, job_id, source_id, saved_paths, file_key)
    return {"job_id": job_id, "status": "running"}
