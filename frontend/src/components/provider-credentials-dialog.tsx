import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { toast } from 'sonner'
import { connections } from '@/lib/api'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface ProviderCredentialsDialogProps {
  open: boolean
  provider: string
  providerName: string
  onClose: () => void
  /** Called after credentials were saved or removed. */
  onChanged?: (configured: boolean) => void
}

/**
 * Lets the signed-in user bring their own provider credentials (e.g. their
 * Pluggy application), so each person sharing an instance connects banks
 * through their own provider account. The secret is write-only.
 */
export function ProviderCredentialsDialog(props: ProviderCredentialsDialogProps) {
  return props.open ? <ProviderCredentialsSession {...props} /> : null
}

function ProviderCredentialsSession({
  open,
  provider,
  providerName,
  onClose,
  onChanged,
}: ProviderCredentialsDialogProps) {
  const { t } = useTranslation()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [configured, setConfigured] = useState(false)
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    connections
      .getProviderCredentials(provider)
      .then((data) => {
        if (cancelled) return
        setConfigured(data.configured)
        setClientId(data.client_id ?? '')
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [provider])

  const handleSave = async () => {
    setError('')
    setSaving(true)
    try {
      await connections.saveProviderCredentials(provider, clientId.trim(), clientSecret.trim())
      toast.success(t('providerCredentials.saved'))
      onChanged?.(true)
      onClose()
    } catch (e: unknown) {
      const status = (e as { response?: { status?: number } })?.response?.status
      setError(status === 422 ? t('providerCredentials.invalid') : t('providerCredentials.saveError'))
    } finally {
      setSaving(false)
    }
  }

  const handleRemove = async () => {
    setSaving(true)
    try {
      await connections.deleteProviderCredentials(provider)
      toast.success(t('providerCredentials.removed'))
      onChanged?.(false)
      onClose()
    } catch {
      setError(t('providerCredentials.saveError'))
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t('providerCredentials.title', { provider: providerName })}</DialogTitle>
          <p className="text-sm text-muted-foreground">
            {t('providerCredentials.description', { provider: providerName })}
          </p>
        </DialogHeader>

        {loading ? (
          <div className="flex justify-center py-8">
            <div className="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          </div>
        ) : (
          <div className="space-y-4 pt-2">
            {provider === 'pluggy' && (
              <ol className="list-decimal pl-5 text-xs text-muted-foreground space-y-1">
                <li>
                  {t('providerCredentials.pluggyStep1')}{' '}
                  <a className="underline" href="https://meu.pluggy.ai" target="_blank" rel="noreferrer">
                    meu.pluggy.ai
                  </a>
                </li>
                <li>
                  {t('providerCredentials.pluggyStep2')}{' '}
                  <a className="underline" href="https://dashboard.pluggy.ai" target="_blank" rel="noreferrer">
                    dashboard.pluggy.ai
                  </a>
                </li>
                <li>{t('providerCredentials.pluggyStep3')}</li>
              </ol>
            )}
            <div className="space-y-1.5">
              <Label htmlFor="provider-client-id">Client ID</Label>
              <Input
                id="provider-client-id"
                value={clientId}
                autoComplete="off"
                onChange={(e) => setClientId(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="provider-client-secret">Client Secret</Label>
              <Input
                id="provider-client-secret"
                type="password"
                value={clientSecret}
                autoComplete="new-password"
                placeholder={configured ? t('providerCredentials.secretKept') : ''}
                onChange={(e) => setClientSecret(e.target.value)}
              />
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
          </div>
        )}

        <DialogFooter className="gap-2">
          {configured && (
            <Button variant="outline" onClick={handleRemove} disabled={saving} className="mr-auto">
              {t('providerCredentials.remove')}
            </Button>
          )}
          <Button variant="outline" onClick={onClose} disabled={saving}>
            {t('common.cancel')}
          </Button>
          <Button onClick={handleSave} disabled={saving || loading || !clientId.trim() || !clientSecret.trim()}>
            {saving ? t('providerCredentials.validating') : t('common.save')}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
