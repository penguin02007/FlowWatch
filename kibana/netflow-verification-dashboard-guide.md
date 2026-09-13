# Operational health dashboard guide

Use this layout to build a clean Kibana dashboard that answers one question quickly: is the NetFlow pipeline healthy?

## 1. Create the index pattern

1. Open Kibana.
2. Go to Stack Management > Index Patterns.
3. Click Create index pattern.
4. Use `elastiflow-*`.
5. Set the primary time field to `@timestamp`.
6. Save it.

## 2. Create a dashboard named "Operational Health"

1. Go to Analytics > Dashboard.
2. Click Create dashboard.
3. Save the dashboard as `Operational Health`.
4. Set the refresh interval to 5s.

## 3. Use a compact 3-panel layout

### Panel A: Flow volume status

- Visualization type: Metric
- Data view: `elastiflow-*`
- Metric: `Unique count` of `flow.src.ip.addr`
- Time range: last 15 minutes
- Title: `Live Source Hosts`

This tells you whether the NetFlow pipeline is active and producing unique source endpoints.

### Panel B: Top bytes by source

- Visualization type: Bar chart
- Metric: `sum(flow.in.bytes)`
- Split by: `flow.src.ip.addr.keyword`
- Sort by descending value
- Limit: 5
- Title: `Top Source IPs by Bytes`

This quickly highlights the heaviest talkers.

### Panel C: Recent flow evidence

- Visualization type: Data table
- Fields: `@timestamp`, `flow.src.ip.addr`, `flow.dst.ip.addr`, `flow.dst.l4.port.id`, `flow.packets`, `flow.in.bytes`
- Sort by `@timestamp` descending
- Limit: 20
- Title: `Recent Flow Records`

This is your operational proof that real NetFlow records are being indexed.

## 4. Optional trend chart

Add one time series below the three summary panels:

- X-axis: `@timestamp`
- Y-axis: `sum(flow.packets)`
- Title: `Flow Volume Over Time`

This helps distinguish healthy steady flow from a drop or a spike.

## 5. Expected operational health signals

A healthy pipeline should show:

- Live Source Hosts > 0
- Top Source IPs by Bytes > 0
- Recent Flow Records with non-empty values
- Flow Volume Over Time increasing or steady instead of flat zero

If the values are zero, the pipeline is not receiving or indexing NetFlow data.
