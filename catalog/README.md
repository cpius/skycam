# Star catalogue

`bsc_v6.csv` — Yale Bright Star Catalogue, 5th revised edition (Hoffleit & Warren 1991), all stars
brighter than V = 6.0, from CDS VizieR table `V/50/catalog` via TAP on 2026-10-19:

    SELECT HR, Name, Vmag, RAJ2000, DEJ2000 FROM "V/50/catalog" WHERE Vmag < 6.0

`Name` keeps VizieR's fixed-width layout: 3-character Flamsteed number, then the Bayer designation
(e.g. ` 21Alp And`). RA/Dec are J2000 degrees.
