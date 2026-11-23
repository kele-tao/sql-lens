-- One row per order plus a per-customer count. The subquery touches the
-- outer order, so it is evaluated once for every surviving order row.
select o.id,
       o.total_cents,
       (select count(*)
        from orders x
        where x.customer_id = o.customer_id) as customer_orders
from orders o
where o.status = 'paid'
order by o.created_at desc;
