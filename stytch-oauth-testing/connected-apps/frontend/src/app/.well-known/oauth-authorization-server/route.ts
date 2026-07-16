// OPTIONAL FALLBACK — not on the happy path for "custom domain + native PRM".
//
// In the topology we use, the Couchbase MCP server publishes PRM pointing at
// your Stytch CUSTOM DOMAIN as the authorization server, and Stytch serves its
// own /.well-known/oauth-authorization-server there. You normally do NOT need
// this route.
//
// Keep it only if you want THIS app's origin to act as the advertised
// authorization server instead (e.g. you point PRM at http://localhost:3000).
// It mirrors the metadata the Stytch examples publish: authorize -> this app,
// token/register -> Stytch.

const STYTCH_DOMAIN = process.env.STYTCH_DOMAIN || '';

export async function GET(request: Request) {
  const baseUrl = new URL(request.url).origin;

  const metadata = {
    issuer: STYTCH_DOMAIN,
    authorization_endpoint: `${baseUrl}/oauth/authorize`,
    token_endpoint: `${STYTCH_DOMAIN}/v1/oauth2/token`,
    registration_endpoint: `${STYTCH_DOMAIN}/v1/oauth2/register`,
    jwks_uri: `${STYTCH_DOMAIN}/.well-known/jwks.json`,
    scopes_supported: ['openid', 'email', 'profile', 'couchbase-mcp:read', 'couchbase-mcp:write'],
    response_types_supported: ['code'],
    response_modes_supported: ['query'],
    grant_types_supported: ['authorization_code', 'refresh_token'],
    token_endpoint_auth_methods_supported: ['none', 'client_secret_post', 'client_secret_basic'],
    code_challenge_methods_supported: ['S256'],
  };

  return new Response(JSON.stringify(metadata), {
    headers: {
      'Content-Type': 'application/json',
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*',
    },
  });
}

export async function OPTIONS() {
  return new Response(null, {
    headers: {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, OPTIONS',
      'Access-Control-Allow-Headers': '*',
    },
  });
}
