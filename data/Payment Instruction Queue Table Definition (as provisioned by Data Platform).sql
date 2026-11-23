-- =============================================================================
-- PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE: provisioning DDL & grants
-- -----------------------------------------------------------------------------
-- Owner        : Olumide Fadairo, Snowflake Platform Engineer
--                Data Platform & Governance, J.P. Morgan
-- Business unit: Global Payments Operations
-- Provisioned  : 2026-11-16
-- Change ref   : DPG-CHG-2026-1147
-- Requested for: The 2026-12-08 load of the 300 checked payment rows built from
--                client_payment_emails_sep_nov_2026.csv
--                (Payments Data Engineering, Sarah Moreau)
--
-- Run as       : ACCOUNTADMIN or SYSADMIN in the target account.
--                If you run it as SYSADMIN, that role also needs
--                CREATE ROLE ON ACCOUNT (granted by USERADMIN) for section 3.
--                For a personal trial account, sign up at
--                https://signup.snowflake.com. The trial login holds
--                ACCOUNTADMIN by default.
--
-- Consumers    : The payment review page reads from this table. Payments
--                Operations analysts work the queue each morning before cut-off.
--
-- Credentials  : Loader connections must read the account, user and password
--                from environment variables (SNOWFLAKE_ACCOUNT, SNOWFLAKE_USER,
--                SNOWFLAKE_PASSWORD). Do not put an account locator, username
--                or password in this file, in code or anywhere in a repository.
--
-- Contract     : Load scripts must write to this table exactly as defined
--                below. Use the same column names, types and NOT NULL flags.
--                Do not create additional tables or schemas.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. Database and schema
-- -----------------------------------------------------------------------------
CREATE DATABASE IF NOT EXISTS PAYMENTS_OPS
    COMMENT = 'Global Payments Operations. Owner: Data Platform & Governance (Olumide Fadairo). Ref DPG-CHG-2026-1147.';

CREATE SCHEMA IF NOT EXISTS PAYMENTS_OPS.PAYMENT_QUEUE
    COMMENT = 'Screened client payment instructions awaiting operations action. Ref DPG-CHG-2026-1147.';


-- -----------------------------------------------------------------------------
-- 2. Warehouse
-- -----------------------------------------------------------------------------
CREATE WAREHOUSE IF NOT EXISTS PAYMENTS_OPS_WH
    WAREHOUSE_SIZE      = 'XSMALL'
    AUTO_SUSPEND        = 60
    AUTO_RESUME         = TRUE
    INITIALLY_SUSPENDED = TRUE
    COMMENT             = 'Compute for payment queue loads and review page reads. Ref DPG-CHG-2026-1147.';


-- -----------------------------------------------------------------------------
-- 3. Loader role
--    Grants : USAGE on database, schema and warehouse.
--             INSERT and SELECT on PAYMENT_INSTRUCTION_QUEUE only.
--             No UPDATE, DELETE, TRUNCATE or CREATE privileges are granted.
--    Snowflake requires the table to exist before table privileges can be
--    granted, so the table-level grant is in section 5, after the CREATE TABLE.
-- -----------------------------------------------------------------------------
CREATE ROLE IF NOT EXISTS PAYMENTS_OPS_LOADER
    COMMENT = 'Pipeline load role for PAYMENT_INSTRUCTION_QUEUE: INSERT and SELECT only. Ref DPG-CHG-2026-1147.';

-- Role hierarchy: SYSADMIN inherits the loader role, so the account's admin
-- login can assume it. No user is named in this file.
GRANT ROLE PAYMENTS_OPS_LOADER TO ROLE SYSADMIN;

GRANT USAGE ON DATABASE  PAYMENTS_OPS                TO ROLE PAYMENTS_OPS_LOADER;
GRANT USAGE ON SCHEMA    PAYMENTS_OPS.PAYMENT_QUEUE  TO ROLE PAYMENTS_OPS_LOADER;
GRANT USAGE ON WAREHOUSE PAYMENTS_OPS_WH             TO ROLE PAYMENTS_OPS_LOADER;


-- -----------------------------------------------------------------------------
-- 4. Payment instruction queue table
--    One row per checked payment email. One column per field of the checked
--    payment table, plus two load-metadata columns.
--    Snowflake records the PRIMARY KEY on EMAIL_ID but does not enforce it.
--    Snowflake does not support CHECK constraints on standard tables, so the
--    allowed values are documented in the column comments.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE (
    EMAIL_ID              VARCHAR(8)     NOT NULL
        COMMENT 'Source email identifier, format EM-00001..EM-00300; matches email_id in client_payment_emails_sep_nov_2026.csv',
    CLIENT_ID             VARCHAR(7)     NOT NULL
        COMMENT 'Client identifier, format CL-1001..CL-1030; matches client_id in client_payment_emails_sep_nov_2026.csv',
    PAYEE_NAME            VARCHAR(200)
        COMMENT 'Extracted payee name; NULL when not extracted',
    PAYMENT_DATE          DATE
        COMMENT 'Extracted payment date, ISO YYYY-MM-DD, within 2026-09-01..2026-12-18; NULL when not extracted',
    AMOUNT                NUMBER(18,2)
        COMMENT 'Extracted payment amount, positive, two decimal places, no currency symbol or thousands separator; NULL when not extracted',
    CURRENCY              VARCHAR(3)
        COMMENT 'ISO currency code: USD|EUR|GBP|CAD|CHF|JPY; NULL when not extracted',
    ACCOUNT_NUMBER        VARCHAR(34)
        COMMENT 'Extracted destination account number, up to 34 characters; NULL when not extracted',
    BANK_DETAIL_CHANGE    BOOLEAN        NOT NULL
        COMMENT 'true|false. true = email asks for payment to an account different from the one on file for CLIENT_ID, including a request quoted in a forwarded thread',
    IS_FORWARDED_THREAD   BOOLEAN        NOT NULL
        COMMENT 'true|false. true = email body contains a quoted earlier message',
    EXTRACTION_STATUS     VARCHAR(20)    NOT NULL
        COMMENT 'Model extraction outcome: ok|unusable_output',
    DISPOSITION           VARCHAR(5)     NOT NULL
        COMMENT 'Queue disposition, held|ready only. held = payment stopped for human review; ready = payment accepted for the queue',
    HOLD_REASON           VARCHAR(30)    NOT NULL
        COMMENT 'bank_detail_change|missing_required_field|invalid_amount|invalid_payment_date|invalid_account_number|unusable_model_output|none',
    GE_VALIDATION_PASSED  BOOLEAN        NOT NULL
        COMMENT 'true|false. Result of the Great Expectations contract for this row',
    LOAD_RUN_ID           VARCHAR(64)    NOT NULL
        COMMENT 'Identifier of the load run that wrote the row',
    LOADED_AT             TIMESTAMP_NTZ  NOT NULL DEFAULT CURRENT_TIMESTAMP()
        COMMENT 'Time the row was written; set by Snowflake default',

    CONSTRAINT PK_PAYMENT_INSTRUCTION_QUEUE PRIMARY KEY (EMAIL_ID)
)
COMMENT = 'Payment instruction queue: checked client payment emails, each held or ready. Read by the payment review page. Owner: Data Platform & Governance (Olumide Fadairo). Ref DPG-CHG-2026-1147.';


-- -----------------------------------------------------------------------------
-- 5. Table-level grants for the loader role (INSERT and SELECT only)
-- -----------------------------------------------------------------------------
GRANT INSERT, SELECT ON TABLE PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE
    TO ROLE PAYMENTS_OPS_LOADER;


-- -----------------------------------------------------------------------------
-- 6. Provisioning check (read-only)
-- -----------------------------------------------------------------------------
SHOW GRANTS TO ROLE PAYMENTS_OPS_LOADER;
DESCRIBE TABLE PAYMENTS_OPS.PAYMENT_QUEUE.PAYMENT_INSTRUCTION_QUEUE;