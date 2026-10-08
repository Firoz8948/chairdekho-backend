-- ChairDekho — PostgreSQL setup (run as superuser postgres)

DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'chairdekho') THEN
    CREATE ROLE chairdekho WITH LOGIN PASSWORD 'chairdekho';
  ELSE
    ALTER ROLE chairdekho WITH LOGIN PASSWORD 'chairdekho';
  END IF;
END
$$;

SELECT 'CREATE DATABASE chairdekho OWNER chairdekho'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'chairdekho')\gexec

GRANT ALL PRIVILEGES ON DATABASE chairdekho TO chairdekho;
