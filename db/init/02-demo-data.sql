-- Demo catalog, warehouses and stock.

INSERT INTO products (id, name, unit_price_cents) VALUES
    (1, 'Notebook 14"',        129900),
    (2, 'Wireless mouse',        2500),
    (3, 'Mechanical keyboard',   8900),
    (4, '27" monitor',          24900),
    (5, 'HD webcam',             5900);

INSERT INTO warehouses (id, name, latitude, longitude) VALUES
    (1, 'Buenos Aires', -34.6118, -58.4173),
    (2, 'Córdoba',      -31.4201, -64.1888),
    (3, 'Rosario',      -32.9442, -60.6505),
    (4, 'Mendoza',      -32.8908, -68.8272);

-- Only Córdoba and Mendoza have webcams, and Rosario has just two monitors.
INSERT INTO stock (warehouse_id, product_id, quantity) VALUES
    (1, 1, 10), (1, 2, 50), (1, 3, 20), (1, 4,  5),
    (2, 1,  5), (2, 2, 30), (2, 3, 10), (2, 4,  8), (2, 5, 15),
    (3, 1,  3), (3, 2, 20), (3, 3,  5), (3, 4,  2),
    (4, 2, 10), (4, 3, 10), (4, 5,  4);
