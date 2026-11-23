-- The query whose real plan is stored in examples/explain_orders.json.
select c.email,
       c.country,
       count(o.id) as orders,
       sum(o.total_cents) as revenue
from analytics.customers as c
join analytics.orders as o on o.customer_id = c.id
where lower(c.email) = 'user42@example.com'
  and o.note like '%gift%'
  and o.status not in (select status from orders where status = 'cancelled')
group by c.email, c.country
order by revenue desc;
