import { IdentityProvider } from '@/components/Auth';

// The OAuth consent screen. Register this URL (http://localhost:3000/oauth/authorize)
// as the project's "Authorization URL" in the Stytch dashboard.
export default function AuthorizePage() {
  return <IdentityProvider />;
}
