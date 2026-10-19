\set ON_ERROR_STOP on
\getenv writer_user POSTGRES_USER
\getenv writer_password POSTGRES_PASSWORD
\getenv reader_user GRAFANA_POSTGRES_USER
\getenv reader_password GRAFANA_POSTGRES_PASSWORD
\getenv database_name POSTGRES_DB

SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L',
              :'writer_user', :'writer_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'writer_user')
\gexec

SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %L',
              :'reader_user', :'reader_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'reader_user')
\gexec

SELECT format('CREATE DATABASE %I OWNER %I', :'database_name', :'writer_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database_name')
\gexec

REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO :"writer_user", :"reader_user";
