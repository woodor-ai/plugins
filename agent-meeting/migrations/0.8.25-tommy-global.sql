-- 0.8.25 cutover: migrate Tommy's AMBridge identity to global (*).
--
-- sessions PK is (project, name). UPDATE project='*' on (AMBridge,Tommy) will
-- hit a PK conflict if (*,Tommy) already exists (e.g. from a partial prior run).
-- Guard: DELETE any pre-existing (*,Tommy) row first, then UPDATE.
--
-- group_members PK is (group_project, group_name, member_project, member_name).
-- Same pattern applies.
--
-- The Resources project row for Tommy is intentionally left untouched.
-- messages history is intentionally not modified (append-only).

BEGIN;

-- sessions
DELETE FROM sessions WHERE project='*' AND name='Tommy';
UPDATE sessions SET project='*' WHERE name='Tommy' AND project='AMBridge';

-- group_members
DELETE FROM group_members WHERE member_project='*' AND member_name='Tommy';
UPDATE group_members SET member_project='*' WHERE member_name='Tommy' AND member_project='AMBridge';

COMMIT;
