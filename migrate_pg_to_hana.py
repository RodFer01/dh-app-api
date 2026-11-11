"""
Migration script: PostgreSQL → SAP HANA Cloud
Reads from local PostgreSQL, writes to HANA Cloud via hdbcli.
Run: python3 migrate_pg_to_hana.py
"""
import os, sys, time
import psycopg2, psycopg2.extras
from hdbcli import dbapi

# ── Connections ───────────────────────────────────────────────────────────────
PG = psycopg2.connect(
    host="localhost", port=5432, dbname="dh_main_app",
    user="postgres", password="postgres"
)
PG.autocommit = True

HANA = dbapi.connect(
    address="67d6c402-ce38-4c49-93a2-318a35191e1f.hna1.prod-us10.hanacloud.ondemand.com",
    port=443, user="DBADMIN", password="Digital@123",
    encrypt=True, sslValidateCertificate=True
)

def hana_exec(sql, ignore_errors=False):
    cur = HANA.cursor()
    try:
        cur.execute(sql)
        HANA.commit()
    except Exception as e:
        HANA.rollback()
        if not ignore_errors:
            raise
        else:
            print(f"  [warn] {str(e)[:120]}")
    finally:
        cur.close()

def hana_exec_many(sql, rows, batch=500):
    cur = HANA.cursor()
    try:
        for i in range(0, len(rows), batch):
            cur.executemany(sql, rows[i:i+batch])
            HANA.commit()
    except Exception as e:
        HANA.rollback()
        raise
    finally:
        cur.close()

def pg_query(sql):
    cur = PG.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(sql)
    return [dict(r) for r in cur.fetchall()]

# ── DDL — HANA table definitions ─────────────────────────────────────────────
# Only the columns needed by the application (skip computed/unused columns)

TABLES_DDL = {

"pipeline": """
CREATE COLUMN TABLE pipeline (
    opportunity_id          NVARCHAR(50),
    snapshot_week           DATE,
    account_name            NVARCHAR(500),
    opp_description         NVARCHAR(1000),
    opp_owner_name          NVARCHAR(200),
    region_lvl3             NVARCHAR(100),
    region_lvl4             NVARCHAR(100),
    solution_area_l1        NVARCHAR(200),
    sub_solution_area_l2    NVARCHAR(200),
    solution_area_l3        NVARCHAR(200),
    solution_area_l4        NVARCHAR(200),
    opp_phase               NVARCHAR(100),
    fc_qualification        NVARCHAR(100),
    closing_qtr             NVARCHAR(20),
    closing_date            DATE,
    drm_category            NVARCHAR(100),
    iss_group_text          NVARCHAR(200),
    deal_qualification_decision NVARCHAR(100),
    deal_qualification_score DECIMAL(10,2),
    local_acct_owner        NVARCHAR(200),
    flsm                    NVARCHAR(200),
    iac                     NVARCHAR(100),
    engagement_type         NVARCHAR(100),
    vat_dh                  NVARCHAR(500),
    vat_full                NVARCHAR(500),
    vat_size                INTEGER,
    acv_usd                 DECIMAL(20,2),
    acv_total_opp           DECIMAL(20,2),
    acv_per_vat             DECIMAL(20,2),
    channel_partner         NVARCHAR(300),
    channel_manager         NVARCHAR(200),
    distribution_channel    NVARCHAR(100),
    opp_origin              NVARCHAR(100),
    age_days                INTEGER,
    untouched_days          INTEGER,
    account_planned_archetype_text NVARCHAR(200),
    sde_current_territory_owner    NVARCHAR(200),
    account_sales_group_text       NVARCHAR(200),
    PRIMARY KEY (opportunity_id, solution_area_l1, sub_solution_area_l2, solution_area_l3, solution_area_l4, snapshot_week)
)""",

"pipeline_last_week": """
CREATE COLUMN TABLE pipeline_last_week (
    opportunity_id          NVARCHAR(50),
    snapshot_week           DATE,
    account_name            NVARCHAR(500),
    opp_owner_name          NVARCHAR(200),
    region_lvl3             NVARCHAR(100),
    solution_area_l1        NVARCHAR(200),
    sub_solution_area_l2    NVARCHAR(200),
    solution_area_l3        NVARCHAR(200),
    solution_area_l4        NVARCHAR(200),
    drm_category            NVARCHAR(100),
    fc_qualification        NVARCHAR(100),
    closing_qtr             NVARCHAR(20),
    acv_usd                 DECIMAL(20,2),
    acv_total_opp           DECIMAL(20,2),
    engagement_type         NVARCHAR(100),
    PRIMARY KEY (opportunity_id, solution_area_l1, sub_solution_area_l2, solution_area_l3, solution_area_l4, snapshot_week)
)""",

"melody": """
CREATE COLUMN TABLE melody (
    activ_id                NVARCHAR(100),
    activ_type              NVARCHAR(100),
    activ_status            NVARCHAR(100),
    acct_name               NVARCHAR(500),
    activ_solution_l2       NVARCHAR(200),
    activ_solution_l3       NVARCHAR(200),
    activ_team_member_role  NVARCHAR(200),
    opp_id                  NVARCHAR(50),
    activ_team_empl_id      NVARCHAR(100),
    activ_team_empl_name    NVARCHAR(200),
    activ_start_date_utc    TIMESTAMP,
    activ_end_date_utc      TIMESTAMP,
    activ_notes             NCLOB,
    opp_main_solution_l2    NVARCHAR(200),
    opp_main_solution_l3    NVARCHAR(200),
    opp_main_solution_l4    NVARCHAR(200),
    activity_count          INTEGER,
    activity_team_count     INTEGER,
    PRIMARY KEY (activ_id, activ_team_empl_id)
)""",

"targets": """
CREATE COLUMN TABLE targets (
    inumber                 NVARCHAR(20) PRIMARY KEY,
    nombre_empleado         NVARCHAR(200),
    manager                 NVARCHAR(200),
    area                    NVARCHAR(100),
    sub_team                NVARCHAR(100),
    acv_booked_annual_target DECIMAL(20,2),
    deals_booked_annual_target INTEGER,
    quota_q1                DECIMAL(20,2),
    quota_q2                DECIMAL(20,2),
    quota_q3                DECIMAL(20,2),
    quota_q4                DECIMAL(20,2),
    deals_q1                INTEGER,
    deals_q2                INTEGER,
    deals_q3                INTEGER,
    deals_q4                INTEGER
)""",

"melody_requests": """
CREATE COLUMN TABLE melody_requests (
    req_id_sub              NVARCHAR(50) PRIMARY KEY,
    req_create_date         DATE,
    req_id                  NVARCHAR(50),
    req_type                NVARCHAR(100),
    opportunity_id          NVARCHAR(50),
    req_status              NVARCHAR(100),
    sub_status              NVARCHAR(100),
    req_role                NVARCHAR(100),
    req_solution            NVARCHAR(200),
    req_industry            NVARCHAR(200),
    requestor               NVARCHAR(200),
    requestor_id            NVARCHAR(50),
    req_processor           NVARCHAR(500),
    req_resource            NVARCHAR(200),
    acct_id                 NVARCHAR(50),
    acct_name               NVARCHAR(500),
    req_status_main         NVARCHAR(100),
    req_task                NVARCHAR(200),
    req_task_action_taken   NVARCHAR(500),
    req_task_owner          NVARCHAR(200),
    req_task_owner_role     NVARCHAR(200),
    req_profit_center       NVARCHAR(100),
    req_role_area           NVARCHAR(200),
    req_last_action_date    DATE,
    uploaded_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"melody_request_comments": """
CREATE COLUMN TABLE melody_request_comments (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    req_id_sub              NVARCHAR(50) NOT NULL,
    author                  NVARCHAR(200),
    body                    NCLOB,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"mr_engagement_status": """
CREATE COLUMN TABLE mr_engagement_status (
    req_id_sub              NVARCHAR(50) PRIMARY KEY,
    engagement_status       NVARCHAR(100),
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"dda": """
CREATE COLUMN TABLE dda (
    opp_id                  NVARCHAR(50) PRIMARY KEY,
    dda_status              NVARCHAR(100),
    range_aging             NVARCHAR(100),
    aging_up_today          INTEGER
)""",

"fx_rates": """
CREATE COLUMN TABLE fx_rates (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    from_currency           NVARCHAR(10) DEFAULT 'USD',
    to_currency             NVARCHAR(10) DEFAULT 'EUR',
    rate                    DECIMAL(20,6),
    rate_date               DATE,
    source                  NVARCHAR(100),
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"app_users": """
CREATE COLUMN TABLE app_users (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email                   NVARCHAR(200) NOT NULL,
    name                    NVARCHAR(200) NOT NULL,
    password_hash           NVARCHAR(200) NOT NULL,
    role                    NVARCHAR(50) DEFAULT 'viewer',
    is_active               BOOLEAN DEFAULT TRUE,
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at           TIMESTAMP,
    created_by              NVARCHAR(200),
    UNIQUE (email)
)""",

"app_permissions": """
CREATE COLUMN TABLE app_permissions (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id                 INTEGER NOT NULL,
    module                  NVARCHAR(100) NOT NULL,
    can_read                BOOLEAN DEFAULT TRUE,
    can_write               BOOLEAN DEFAULT FALSE,
    granted_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    granted_by              NVARCHAR(200),
    UNIQUE (user_id, module)
)""",

"app_access_logs": """
CREATE COLUMN TABLE app_access_logs (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id                 INTEGER,
    email                   NVARCHAR(200),
    action                  NVARCHAR(100) NOT NULL,
    module                  NVARCHAR(100),
    detail                  NVARCHAR(1000),
    ip_address              NVARCHAR(50),
    user_agent              NVARCHAR(500),
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"scheduler_period": """
CREATE COLUMN TABLE scheduler_period (
    id                      INTEGER PRIMARY KEY,
    start_date              DATE NOT NULL,
    end_date                DATE NOT NULL,
    freq_days               INTEGER DEFAULT 20,
    duration_min            INTEGER DEFAULT 60,
    meet_time               NVARCHAR(10) DEFAULT '10:00',
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"scheduler_email_map": """
CREATE COLUMN TABLE scheduler_email_map (
    name                    NVARCHAR(200) PRIMARY KEY,
    email                   NVARCHAR(200) NOT NULL,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"scheduler_quota_carriers": """
CREATE COLUMN TABLE scheduler_quota_carriers (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    qc                      NVARCHAR(200) NOT NULL,
    flsm                    NVARCHAR(200),
    geo                     NVARCHAR(100),
    lob                     NVARCHAR(200),
    sde                     NVARCHAR(200),
    dsa_spoc                NVARCHAR(200),
    dva                     NVARCHAR(200),
    source                  NVARCHAR(100) DEFAULT 'pipeline_db',
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (qc, flsm, lob)
)""",

"scheduler_meetings": """
CREATE COLUMN TABLE scheduler_meetings (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    qc                      NVARCHAR(200) NOT NULL,
    flsm                    NVARCHAR(200),
    lob                     NVARCHAR(200),
    meeting_date            DATE NOT NULL,
    status                  NVARCHAR(50) DEFAULT 'pending',
    event_id                NVARCHAR(500),
    web_link                NVARCHAR(1000),
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (qc, flsm, lob, meeting_date)
)""",

"next_steps": """
CREATE COLUMN TABLE next_steps (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    opportunity_id          NVARCHAR(50),
    description             NCLOB,
    due_date                DATE,
    status                  NVARCHAR(50) DEFAULT 'open',
    owner                   NVARCHAR(200),
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"comments": """
CREATE COLUMN TABLE comments (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    opportunity_id          NVARCHAR(50),
    body                    NCLOB,
    author                  NVARCHAR(200),
    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"presales": """
CREATE COLUMN TABLE presales (
    opp_id                  NVARCHAR(50) PRIMARY KEY,
    presales_lead_status    NVARCHAR(100),
    presales_lead_comments  NCLOB,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)""",

"melody_request_upload_log": """
CREATE COLUMN TABLE melody_request_upload_log (
    id                      INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    uploaded_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    filename                NVARCHAR(500),
    mode                    NVARCHAR(50),
    rows_in_file            INTEGER,
    rows_inserted           INTEGER,
    rows_updated            INTEGER,
    rows_unchanged          INTEGER,
    rows_removed            INTEGER,
    uploaded_by             NVARCHAR(200)
)""",

}

# ── Migration functions ───────────────────────────────────────────────────────

def create_tables():
    print("\n=== Creating tables ===")
    for name, ddl in TABLES_DDL.items():
        try:
            hana_exec(f"DROP TABLE {name}")
        except:
            pass
        try:
            hana_exec(ddl)
            print(f"  ✓ {name}")
        except Exception as e:
            print(f"  ✗ {name}: {e}")


def migrate_table(pg_table, hana_table, pg_cols, hana_cols=None, batch_size=1000, transform=None):
    if hana_cols is None:
        hana_cols = pg_cols

    print(f"\n  Migrating {pg_table}...")
    t0 = time.time()

    cur_pg = PG.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur_pg.execute(f"SELECT {', '.join(pg_cols)} FROM {pg_table}")

    col_str = ', '.join(hana_cols)
    ph_str  = ', '.join(['?'] * len(hana_cols))
    sql_ins = f"INSERT INTO {hana_table} ({col_str}) VALUES ({ph_str})"

    total = 0
    batch = []

    def flush():
        nonlocal total, batch
        if not batch:
            return
        cur_h = HANA.cursor()
        try:
            cur_h.executemany(sql_ins, batch)
            HANA.commit()
            total += len(batch)
        except Exception as e:
            HANA.rollback()
            print(f"    [error] batch insert: {str(e)[:200]}")
        finally:
            cur_h.close()
        batch = []

    while True:
        rows = cur_pg.fetchmany(batch_size)
        if not rows:
            break
        for r in rows:
            row_vals = [r[c] for c in pg_cols]
            if transform:
                row_vals = transform(row_vals, dict(r))
            batch.append(row_vals)
        flush()

    elapsed = time.time() - t0
    print(f"    {total} rows  ({elapsed:.1f}s)")
    return total


def run_migration():
    create_tables()

    print("\n=== Migrating data ===")

    # app_users — handle IDENTITY pk
    rows = pg_query("SELECT email, name, password_hash, role, is_active, created_at, last_login_at, created_by FROM app_users")
    if rows:
        cur_h = HANA.cursor()
        for r in rows:
            cur_h.execute("""
                INSERT INTO app_users (email, name, password_hash, role, is_active, created_at, last_login_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, [r['email'], r['name'], r['password_hash'], r['role'],
                  bool(r['is_active']), r['created_at'], r['last_login_at'], r['created_by']])
        HANA.commit()
        cur_h.close()
        print(f"  ✓ app_users: {len(rows)} rows")

    # app_permissions (need user_id mapping)
    rows = pg_query("""
        SELECT u.email, p.module, p.can_read, p.can_write, p.granted_by
        FROM app_permissions p
        JOIN app_users u ON u.id = p.user_id
    """)
    if rows:
        cur_h = HANA.cursor()
        for r in rows:
            cur_h.execute("SELECT id FROM app_users WHERE email = ?", [r['email']])
            uid_row = cur_h.fetchone()
            if uid_row:
                cur_h.execute("""
                    INSERT INTO app_permissions (user_id, module, can_read, can_write, granted_by)
                    VALUES (?, ?, ?, ?, ?)
                """, [uid_row[0], r['module'], bool(r['can_read']), bool(r['can_write']), r['granted_by']])
        HANA.commit()
        cur_h.close()
        print(f"  ✓ app_permissions: {len(rows)} rows")

    # Simple tables
    simple = [
        ("fx_rates",  ["from_currency","to_currency","rate","rate_date","created_at"]),
        ("targets",   ["inumber","nombre_empleado","manager","area","sub_team",
                       "acv_booked_annual_target","deals_booked_annual_target",
                       "quota_q1","quota_q2","quota_q3","quota_q4",
                       "deals_q1","deals_q2","deals_q3","deals_q4"]),
        ("dda",       ["opp_id","dda_status","range_aging","aging_up_today"]),
        ("presales",  ["opp_id","presales_lead_status","presales_lead_comments"]),
        ("scheduler_period",         ["id","start_date","end_date","freq_days","duration_min","meet_time"]),
        ("scheduler_email_map",      ["name","email"]),
        ("scheduler_quota_carriers", ["qc","flsm","geo","lob","sde","dsa_spoc","dva","source"]),
        ("scheduler_meetings",       ["qc","flsm","lob","meeting_date","status","event_id","web_link"]),
        ("mr_engagement_status",     ["req_id_sub","engagement_status"]),
        ("melody_request_upload_log",["filename","mode","rows_in_file","rows_inserted",
                                      "rows_updated","rows_unchanged","rows_removed","uploaded_by"]),
        ("next_steps",  ["opportunity_id","description","due_date","status","owner"]),
        ("comments",    ["opportunity_id","body","author"]),
    ]
    for table, cols in simple:
        migrate_table(table, table, cols)

    # melody_requests
    mr_cols = ["req_id_sub","req_create_date","req_id","req_type","opportunity_id",
               "req_status","sub_status","req_role","req_solution","req_industry",
               "requestor","requestor_id","req_processor","req_resource",
               "acct_id","acct_name","req_status_main","req_task","req_task_action_taken",
               "req_task_owner","req_task_owner_role","req_profit_center","req_role_area",
               "req_last_action_date"]
    migrate_table("melody_requests", "melody_requests", mr_cols)

    # melody_request_comments
    migrate_table("melody_request_comments", "melody_request_comments",
                  ["req_id_sub","author","body","created_at","updated_at"])

    # melody
    mel_cols = ["activ_id","activ_type","activ_status","acct_name",
                "activ_solution_l2","activ_solution_l3","activ_team_member_role",
                "opp_id","activ_team_empl_id","activ_team_empl_name",
                "activ_start_date_utc","activ_end_date_utc","activ_notes",
                "opp_main_solution_l2","opp_main_solution_l3","opp_main_solution_l4",
                "activity_count","activity_team_count"]
    migrate_table("melody", "melody", mel_cols)

    # pipeline — largest table, batch carefully
    pip_cols = ["opportunity_id","snapshot_week","account_name","opp_description",
                "opp_owner_name","region_lvl3","region_lvl4",
                "solution_area_l1","sub_solution_area_l2","solution_area_l3","solution_area_l4",
                "opp_phase","fc_qualification","closing_qtr","closing_date","drm_category",
                "iss_group_text","deal_qualification_decision","deal_qualification_score",
                "local_acct_owner","flsm","iac","engagement_type",
                "vat_dh","vat_full","vat_size","acv_usd","acv_total_opp","acv_per_vat",
                "channel_partner","channel_manager","distribution_channel",
                "opp_origin","age_days","untouched_days",
                "account_planned_archetype_text","sde_current_territory_owner","account_sales_group_text"]
    migrate_table("pipeline", "pipeline", pip_cols, batch_size=500)

    print("\n✅ Migration complete!")


if __name__ == "__main__":
    try:
        run_migration()
    finally:
        PG.close()
        HANA.close()
