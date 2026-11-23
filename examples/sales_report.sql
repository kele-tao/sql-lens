-- Monthly revenue by country, used by the finance dashboard.
select c.country,
       count(o.id) as orders,
       sum(o.total_cents) as revenue
from orders o, customers c
where o.customer_id = c.id
  and lower(c.email) like '%@example.com'
  and o.status not in (select status from orders where status = 'cancelled')
order by revenue desc;
