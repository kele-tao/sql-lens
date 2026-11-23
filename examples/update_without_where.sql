-- Deactivate stale accounts. No WHERE: this locks and rewrites every row.
update accounts
set locked = true,
    locked_at = now();
