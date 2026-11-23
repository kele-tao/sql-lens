-- Reference file: already formatted by `sql-lens format`.
SELECT
  c.country,
  count(o.id) AS orders,
  sum(o.total_cents) AS revenue
FROM analytics.customers AS c
JOIN analytics.orders AS o
  ON o.customer_id = c.id
WHERE o.status = 'paid'
  AND c.country IN ('DE', 'US', 'JP')
GROUP BY c.country
ORDER BY revenue DESC
LIMIT 25;
