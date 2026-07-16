import { Authenticate } from '@/components/Auth';

// OAuth (e.g. Google) redirect callback. Only exercised if you enable the
// oauth product in Auth.tsx. Email-OTP login does not use this route.
export default function AuthenticatePage() {
  return <Authenticate />;
}
