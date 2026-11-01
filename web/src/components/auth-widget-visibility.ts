import type { AuthMeResponse } from '@/lib/api-auth'

export function shouldHideAuthWidget(identity: AuthMeResponse): boolean {
  return identity.provider === 'loopback'
}
