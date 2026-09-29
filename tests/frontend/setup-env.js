// Runs before every frontend test file, in both apps.
//
// The API clients read their base URL once, when the module loads, so it is
// fixed here rather than in each test: every request goes to the same fake
// origin whatever the developer's `.env.local` says, and no test can reach a
// real API.
process.env.NEXT_PUBLIC_API_URL = "http://api.test";
process.env.NEXT_PUBLIC_AUTH_API_URL = "http://api.test";
