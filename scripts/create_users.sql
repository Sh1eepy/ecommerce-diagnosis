-- ============================================================
-- 电商商品经营异常诊断 Agent
-- 建库 + 建账号 + 权限脚本（用 MySQL root 执行一次）
--
-- ⚠️ 安全提示：本文件【禁止】写入真实密码！
-- 请将下方两个占位符替换为你的强密码后再执行，例如：
--   SET @APP_PW = '你的强密码';    SET @RO_PW = '你的强密码';
-- 真实密码只应保存在项目根目录的 .env 文件中（已被 .gitignore 忽略）。
--
-- 权限设计：
--   agent_app（写账号）：仅供服务层/数据导入使用，无 DROP 权限
--   agent_ro （只读账号）：仅供 Agent Tool 使用，仅 SELECT
--   两条铁律：
--     1. Agent 永远只通过 agent_ro 连接（无法写库）
--     2. agent_ro 无 INSERT/UPDATE/DELETE/DROP 权限
-- ============================================================

SET @APP_PW = '__REPLACE_WITH_APP_PASSWORD__';
SET @RO_PW  = '__REPLACE_WITH_RO_PASSWORD__';

CREATE DATABASE IF NOT EXISTS ecommerce_diagnosis
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

-- ---------- 写账号（服务层 / 数据导入） ----------
-- 注意：agent_app 是"系统自己的写路径"（数据导入/任务系统/建表），需要 DDL+DML；
--       Agent 永远不会通过该账号连接。
CREATE USER IF NOT EXISTS 'agent_app'@'localhost' IDENTIFIED BY @APP_PW;
CREATE USER IF NOT EXISTS 'agent_app'@'%' IDENTIFIED BY @APP_PW;
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES, DROP
  ON ecommerce_diagnosis.* TO 'agent_app'@'localhost';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, INDEX, REFERENCES, DROP
  ON ecommerce_diagnosis.* TO 'agent_app'@'%';

-- ---------- 只读账号（Agent Tool 专用） ----------
CREATE USER IF NOT EXISTS 'agent_ro'@'localhost' IDENTIFIED BY @RO_PW;
CREATE USER IF NOT EXISTS 'agent_ro'@'%' IDENTIFIED BY @RO_PW;
GRANT SELECT ON ecommerce_diagnosis.* TO 'agent_ro'@'localhost';
GRANT SELECT ON ecommerce_diagnosis.* TO 'agent_ro'@'%';

FLUSH PRIVILEGES;

-- 验证：
--   SELECT User, Host FROM mysql.user WHERE User IN ('agent_app','agent_ro');
--   SHOW GRANTS FOR 'agent_ro'@'localhost';
