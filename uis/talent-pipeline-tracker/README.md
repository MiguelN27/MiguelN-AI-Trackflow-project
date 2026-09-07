TrackFlow Operations is a [Next.js](https://nextjs.org) interface for the candidate pipeline and incident file analysis.

## Getting Started

Install dependencies, configure both API URLs, then run the development server. Candidate requests go directly to the external 4Geeks API, while `/api/*` incident requests are proxied through Next.js to Flask:

```bash
npm install
NEXT_PUBLIC_API_URL=https://playground.4geeks.com/tracker/api/v1 \
BACKEND_API_URL=http://localhost:5500 \
npm run dev
```

The candidate tracker is available at [http://localhost:3000](http://localhost:3000), and incident analysis is available at [http://localhost:3000/incidents](http://localhost:3000/incidents). The Incident Analysis page uploads CSV files to `POST /api/incidents/analyze` and downloads the result from the export URL returned by that endpoint. `NEXT_PUBLIC_API_URL` remains supported for existing candidate API calls.

For subsequent runs, start the app with:

```bash
npm run dev
```
