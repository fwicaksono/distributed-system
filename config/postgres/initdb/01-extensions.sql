-- Runs ONCE, when the data volume is empty (docker-entrypoint-initdb.d semantics).
-- pg_stat_statements: aggregated statistics per normalized query — the first place to look
-- when "the database is slow":
--   SELECT calls, mean_exec_time, query FROM pg_stat_statements ORDER BY total_exec_time DESC LIMIT 10;
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
