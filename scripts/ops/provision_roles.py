"""One-time DBA-only database least-privilege grants for staging service identities.

Requires PostgreSQL owner/DBA DATABASE_URL and password env ROLE_PASSWORD_<ROLE>.
Do not run from public application containers or print passwords.
"""
from __future__ import annotations
import os
from urllib.parse import urlsplit

ROLE_TABLES = {
    'gateway': {'users':'SELECT', 'market_latest':'SELECT','stream_status':'SELECT',
       'market_data_gaps':'SELECT','instruments':'SELECT','audit_logs':'SELECT,INSERT',
       'paper_accounts':'SELECT,INSERT,UPDATE','paper_ledger':'SELECT,INSERT',
       'paper_strategies':'SELECT,INSERT,UPDATE','historical_bars':'SELECT,INSERT','historical_manifests':'SELECT,INSERT',
       'backtest_runs':'SELECT,INSERT',
       'exchange_accounts':'SELECT,UPDATE','exchange_incidents':'SELECT,UPDATE',
       'exchange_order_intents':'SELECT,INSERT,UPDATE','exchange_order_events':'SELECT,INSERT',
       'exchange_fills':'SELECT', 'exchange_stream_events':'SELECT',
       'governance_requests':'SELECT,INSERT,UPDATE','governance_events':'SELECT,INSERT',
       'research_jobs':'SELECT,INSERT','research_models':'SELECT,UPDATE',
       'research_campaigns':'SELECT,INSERT,UPDATE','shadow_predictions':'SELECT',
       'operations_readiness':'SELECT,INSERT','backup_attestations':'SELECT', 'operations_incidents':'SELECT,UPDATE',
       'service_heartbeats':'SELECT'},
    'collector': {'market_events':'SELECT,INSERT','market_latest':'SELECT,INSERT,UPDATE',
       'aggregate_trade_offsets':'SELECT,INSERT,UPDATE','market_data_gaps':'SELECT,INSERT',
       'outbox_events':'INSERT','historical_bars':'SELECT,INSERT','stream_status':'SELECT,INSERT,UPDATE','instruments':'SELECT,INSERT,UPDATE',
       'service_heartbeats':'INSERT,UPDATE'},
    'outbox': {'outbox_events':'SELECT,UPDATE','service_heartbeats':'INSERT,UPDATE'},
    'paper': {'paper_accounts':'SELECT,UPDATE','paper_ledger':'SELECT,INSERT',
       'paper_strategies':'SELECT,UPDATE','market_latest':'SELECT','market_events':'SELECT',
       'service_heartbeats':'INSERT,UPDATE'},
    'research': {'research_jobs':'SELECT,INSERT,UPDATE','research_models':'SELECT,INSERT,UPDATE',
       'research_campaigns':'SELECT,UPDATE','shadow_predictions':'SELECT,INSERT,UPDATE',
       'historical_bars':'SELECT','service_heartbeats':'INSERT,UPDATE'},
    'exchange': {'exchange_accounts':'SELECT,INSERT,UPDATE',
       'exchange_incidents':'SELECT,INSERT,UPDATE',
       'exchange_order_intents':'SELECT,UPDATE','exchange_order_events':'INSERT',
       'exchange_fills':'SELECT,INSERT','exchange_trade_cursors':'SELECT,INSERT,UPDATE',
       'exchange_stream_events':'INSERT','service_heartbeats':'INSERT,UPDATE'},
    'operations': {'service_heartbeats':'SELECT,INSERT,UPDATE',
       'operations_readiness':'SELECT,INSERT','backup_attestations':'SELECT', 'operations_incidents':'SELECT,INSERT',
       'stream_status':'SELECT','research_models':'SELECT','shadow_predictions':'SELECT',
       'audit_logs':'SELECT','market_data_gaps':'SELECT',
       'exchange_incidents':'SELECT','exchange_order_intents':'SELECT'},
}

ROLE_TABLES['gateway'].update({
    'live_accounts':'SELECT,UPDATE', 'live_authorizations':'SELECT,INSERT,UPDATE',
    'live_authorization_events':'SELECT,INSERT','live_order_intents':'SELECT,INSERT,UPDATE',
    'live_order_events':'SELECT,INSERT','live_incidents':'SELECT,UPDATE',
    'live_automation':'SELECT,INSERT,UPDATE','live_fills':'SELECT',
})
ROLE_TABLES['live']={
    'live_accounts':'SELECT,UPDATE','live_authorizations':'SELECT',
    'live_order_intents':'SELECT,INSERT,UPDATE','live_order_events':'SELECT,INSERT',
    'live_incidents':'SELECT,INSERT,UPDATE','live_fills':'SELECT,INSERT',
    'live_fill_cursors':'SELECT,INSERT,UPDATE','live_automation':'SELECT,UPDATE',
    'operations_readiness':'SELECT','operations_incidents':'SELECT',
    'exchange_incidents':'SELECT','exchange_order_intents':'SELECT',
    'instruments':'SELECT','historical_bars':'SELECT',
    'service_heartbeats':'SELECT,INSERT,UPDATE',
}



def main():
    import psycopg
    from psycopg import sql
    url = os.environ.get('DATABASE_URL')
    if not url:
        raise RuntimeError('DBA DATABASE_URL required')
    password = urlsplit(url).password or ''
    if len(password) < 32 or password.startswith('REPLACE_') or password=='local_dev_only':
        raise RuntimeError('Unsafe DBA password for staging')
    with psycopg.connect(url, autocommit=True) as conn:
        known = {x[0] for x in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
        for service, tables in ROLE_TABLES.items():
            missing = set(tables) - known
            if missing:
                raise RuntimeError(f'{service}: missing tables from migration: {sorted(missing)}')
            role = 'trader_' + service
            password = os.environ.get(f'ROLE_PASSWORD_{service.upper()}','')
            if len(password) < 32 or password.startswith('REPLACE_'):
                raise RuntimeError(f'Missing safe password for {role}')
            if not conn.execute('SELECT 1 FROM pg_roles WHERE rolname=%s',(role,)).fetchone():
                conn.execute(sql.SQL('CREATE ROLE {} LOGIN').format(sql.Identifier(role)))
            conn.execute(sql.SQL('ALTER ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(role),sql.Literal(password)))
            conn.execute(sql.SQL('REVOKE ALL ON SCHEMA public FROM {}').format(sql.Identifier(role)))
            conn.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
            conn.execute(sql.SQL('REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {}').format(sql.Identifier(role)))
            conn.execute(sql.SQL('REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {}').format(sql.Identifier(role)))
            for table, grants in tables.items():
                actions = sql.SQL(',').join(sql.SQL(x) for x in grants.split(','))
                conn.execute(sql.SQL('GRANT {} ON {} TO {}').format(
                    actions,sql.Identifier(table),sql.Identifier(role)))
            # Sequences for tables requiring INSERT; full sequence owner access never granted.
            insertables=[t for t,g in tables.items() if 'INSERT' in g]
            for table in insertables:
                rows=conn.execute('''SELECT pg_get_serial_sequence(%s, a.attname)
                  FROM pg_attribute a JOIN pg_class c ON a.attrelid=c.oid
                  WHERE c.relname=%s AND a.attidentity<>'' AND a.attnum>0''',
                  (f'public.{table}',table)).fetchall()
                # Identity sequences are protected by table INSERT; explicit sequence grants
                # only when PostgreSQL reports a regular serial sequence.
                for seq in (r[0] for r in rows if r[0]):
                    conn.execute(sql.SQL('GRANT USAGE ON SEQUENCE {} TO {}').format(
                        sql.Identifier(*seq.split('.')),sql.Identifier(role)))
            print(f'Provisioned {role} (table grants: {len(tables)})')


if __name__=='__main__':
    main()
