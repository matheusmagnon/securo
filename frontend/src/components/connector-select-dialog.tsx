import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { connections } from '@/lib/api'
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Building2, KeyRound } from 'lucide-react'
import { ProviderCredentialsDialog } from '@/components/provider-credentials-dialog'

export interface Provider {
  name: string
  display_name: string
  description: string
  flow_type: string
  configured: boolean
  requires_institution_select?: boolean
  supports_asset_sync?: boolean
  supports_user_credentials?: boolean
  has_user_credentials?: boolean
}

interface ConnectorSelectDialogProps {
  open: boolean
  onClose: () => void
  onSelect: (provider: Provider) => void
}

export function ConnectorSelectDialog(props: ConnectorSelectDialogProps) {
  return props.open ? <ConnectorSelectSession {...props} /> : null
}

function ConnectorSelectSession({ open, onClose, onSelect }: ConnectorSelectDialogProps) {
  const { t } = useTranslation()
  const [providers, setProviders] = useState<Provider[]>([])
  const [loading, setLoading] = useState(true)
  const [credentialsFor, setCredentialsFor] = useState<Provider | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    connections.getProviders().then((data) => {
      if (cancelled) return
      setProviders(data)
      setLoading(false)
    }).catch(() => {
      if (cancelled) return
      setProviders([])
      setLoading(false)
    })
    return () => { cancelled = true }
  }, [reloadKey])

  if (credentialsFor) {
    return (
      <ProviderCredentialsDialog
        open
        provider={credentialsFor.name}
        providerName={credentialsFor.display_name}
        onClose={() => setCredentialsFor(null)}
        onChanged={() => setReloadKey((k) => k + 1)}
      />
    )
  }

  return (
    <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>{t('accounts.selectConnector')}</DialogTitle>
          <p className="text-sm text-muted-foreground">{t('accounts.selectConnectorDesc')}</p>
        </DialogHeader>
        <div className="space-y-2 pt-2">
          {loading ? (
            <div className="flex justify-center py-8">
              <div className="h-5 w-5 animate-spin rounded-full border-2 border-primary border-t-transparent" />
            </div>
          ) : providers.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-8">
              {t('accounts.noConnectorsAvailable')}
            </p>
          ) : (
            providers.map((p) => (
              <div key={p.name} className="space-y-1">
              <button
                disabled={!p.configured && !p.supports_user_credentials}
                onClick={() => {
                  if (!p.configured) {
                    // Not set up on the instance: let the user bring their own.
                    setCredentialsFor(p)
                    return
                  }
                  onSelect(p)
                  onClose()
                }}
                className={`w-full flex items-start gap-3 rounded-lg border p-4 text-left transition-colors ${
                  p.configured || p.supports_user_credentials
                    ? 'border-border hover:border-primary hover:bg-muted/50 cursor-pointer'
                    : 'border-border/50 opacity-60 cursor-not-allowed'
                }`}
              >
                <div className="w-9 h-9 rounded-lg bg-muted flex items-center justify-center shrink-0 mt-0.5">
                  <Building2 size={16} className="text-muted-foreground" />
                </div>
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium text-foreground">{p.display_name}</p>
                  <p className="text-xs text-muted-foreground mt-0.5">{t(`accounts.providers.${p.name}.description`, p.description)}</p>
                  {!p.configured && (
                    <p className="text-xs text-amber-600 mt-1.5">
                      {p.supports_user_credentials
                        ? t('providerCredentials.needsOwn')
                        : t('accounts.connectorNotConfigured')}
                    </p>
                  )}
                </div>
              </button>
              {p.supports_user_credentials && p.configured && (
                <button
                  type="button"
                  onClick={() => setCredentialsFor(p)}
                  className="flex items-center gap-1.5 px-1 text-xs text-muted-foreground hover:text-foreground"
                >
                  <KeyRound size={12} />
                  {p.has_user_credentials
                    ? t('providerCredentials.usingOwn')
                    : t('providerCredentials.useOwn')}
                </button>
              )}
              </div>
            ))
          )}
        </div>
      </DialogContent>
    </Dialog>
  )
}
