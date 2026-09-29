\getenv gateway_password GATEWAY_DB_PASSWORD
\getenv tools_password TOOLS_DB_PASSWORD
CREATE ROLE toolgate_gateway LOGIN PASSWORD :'gateway_password';
CREATE ROLE toolgate_tools LOGIN PASSWORD :'tools_password';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA gateway AUTHORIZATION toolgate_gateway;
CREATE SCHEMA tools AUTHORIZATION toolgate_tools;
CREATE DATABASE toolgate_test;
\connect toolgate_test
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA gateway AUTHORIZATION toolgate_gateway;
CREATE SCHEMA tools AUTHORIZATION toolgate_tools;
