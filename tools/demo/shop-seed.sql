-- Demo data for the database explorer: a wide table, JSON, long text, binary, a one-column table, a view and a
-- table without a primary key — every case the grid and the row panel have to handle.
create table customers (
  id serial primary key, name text not null, email text unique, country text default 'IN',
  created_at timestamptz not null default now(), profile jsonb, notes text, avatar bytea, active boolean default true
);
insert into customers (name, email, country, profile, notes, avatar)
select 'Customer ' || g, 'customer' || g || '@example.com', (array['IN','DE','US','JP','BR'])[1 + g % 5],
       jsonb_build_object('plan', (array['free','pro','team'])[1 + g % 3], 'seats', g % 7, 'tags', jsonb_build_array('a', 'b')),
       case when g % 4 = 0 then null else repeat('A long note about this customer. ', 3 + g % 40) end,
       case when g % 3 = 0 then decode(repeat('89504e47', 200 + g), 'hex') end
from generate_series(1, 64) g;
create table orders (
  id bigserial primary key, customer_id int references customers(id), status text default 'new', total numeric(10,2),
  currency char(3) default 'EUR', items int, discount numeric(5,2), shipping_name text, shipping_street text,
  shipping_city text, shipping_zip text, shipping_country text, carrier text, tracking_no text, paid_at timestamptz,
  shipped_at timestamptz, delivered_at timestamptz, channel text, coupon text, gift boolean default false,
  weight_kg real, notes text, created_at timestamptz default now(), updated_at timestamptz default now()
);
insert into orders (customer_id, status, total, items, discount, shipping_name, shipping_street, shipping_city,
                    shipping_zip, shipping_country, carrier, tracking_no, paid_at, channel, coupon, weight_kg)
select 1 + g % 64, (array['new','paid','shipped','delivered','refunded'])[1 + g % 5], (g * 13.37)::numeric(10,2),
       1 + g % 9, (g % 4) * 2.5, 'Customer ' || (1 + g % 64), g || ' Long Street Name', 'City ' || g % 12, lpad((g * 97)::text, 6, '0'),
       'DE', (array['DHL','UPS','FedEx'])[1 + g % 3], 'TRK' || lpad(g::text, 10, '0'), now() - g * interval '3 hours',
       (array['web','app','store'])[1 + g % 3], case when g % 5 = 0 then 'SPRING' || g end, (g % 30) / 3.0
from generate_series(1, 320) g;
create table schema_migrations (v integer primary key);
insert into schema_migrations select generate_series(0, 9);
create table audit_log (at timestamptz default now(), actor text, action text);
insert into audit_log (actor, action) select 'user' || g, 'login' from generate_series(1, 5) g;
create view big_spenders as select c.name, sum(o.total) as spent from customers c join orders o on o.customer_id = c.id
  group by c.name order by 2 desc;
