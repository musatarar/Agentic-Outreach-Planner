import { useEffect, useMemo, useState } from 'react';
import { errorMessage } from '../api/client';
import {
  fetchLLMCatalog,
  fetchLLMConfig,
  saveLLMConfig,
  testLLMConfig,
} from '../api/endpoints';
import type {
  LLMCatalog,
  LLMConfig,
  LLMConfigInput,
  LLMModel,
  LLMProvider,
  LLMTestResult,
} from '../api/types';
import { EmptyState, ErrorMessage } from '../components/Messages';
import { PageHeader } from '../components/PageHeader';
import { TierBadge } from '../components/TierBadge';

/** Best-effort mapping from provider key to the env var it reads, for the
 * "using X from the environment" message. Falls back to a guess for any
 * provider the catalog adds later. */
const ENV_VAR_NAMES: Record<string, string> = {
  claude: 'ANTHROPIC_API_KEY',
  groq: 'GROQ_API_KEY',
  chatgpt: 'OPENAI_API_KEY',
  deepseek: 'DEEPSEEK_API_KEY',
};

function envVarNameFor(provider: string): string {
  return ENV_VAR_NAMES[provider] ?? `${provider.toUpperCase()}_API_KEY`;
}

const ERROR_KIND_MESSAGES: Record<string, string> = {
  auth: 'That key was rejected. Check you copied the whole thing.',
  rate_limit: 'Rate limited by the provider. Wait a moment and try again.',
  unknown_model: "That model isn't recognized by the provider. Double-check the model ID.",
  network: "Couldn't reach the provider. Check your connection and try again.",
};

function humanTestError(result: Extract<LLMTestResult, { ok: false }>): string {
  return ERROR_KIND_MESSAGES[result.error_kind] ?? result.message;
}

const compactNumber = new Intl.NumberFormat('en', { notation: 'compact' });

function formatModelSummary(model: LLMModel): string {
  return (
    `${compactNumber.format(model.context_window)} context · ` +
    `$${model.input_price_per_mtok_usd}/$${model.output_price_per_mtok_usd} per Mtok`
  );
}

function sortedProviders(catalog: LLMCatalog): LLMProvider[] {
  return [...catalog.providers].sort((a, b) => (a.sort_order ?? 0) - (b.sort_order ?? 0));
}

interface ProviderCardProps {
  provider: LLMProvider;
  selected: boolean;
  onSelect: () => void;
}

function ProviderCard({ provider, selected, onSelect }: ProviderCardProps) {
  return (
    <button
      type="button"
      className={`provider-card${selected ? ' selected' : ''}`}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <span className="provider-label">{provider.label}</span>
      {provider.key === 'groq' && <span className="free-tag">Free</span>}
    </button>
  );
}

interface ModelCardProps {
  model: LLMModel;
  selected: boolean;
  onSelect: () => void;
}

function ModelCard({ model, selected, onSelect }: ModelCardProps) {
  return (
    <button
      type="button"
      className={`model-card${selected ? ' selected' : ''}`}
      aria-pressed={selected}
      onClick={onSelect}
    >
      <div className="model-card-header">
        <span className="model-label">{model.label}</span>
        <TierBadge tier={model.tier} />
      </div>
      <div className="model-summary">{formatModelSummary(model)}</div>
      {model.notes && <div className="model-notes">{model.notes}</div>}
    </button>
  );
}

export function SettingsPage() {
  const [catalog, setCatalog] = useState<LLMCatalog | null>(null);
  const [config, setConfig] = useState<LLMConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [selectedProvider, setSelectedProvider] = useState('');
  const [selectedModel, setSelectedModel] = useState('');
  const [maxTokens, setMaxTokens] = useState(0);

  // Key editing has three states: showing the masked stored key (default when
  // has_key is true), actively replacing it (editingKey), or marked to clear.
  const [editingKey, setEditingKey] = useState(false);
  const [clearKey, setClearKey] = useState(false);
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [savedJustNow, setSavedJustNow] = useState(false);

  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<LLMTestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    Promise.all([fetchLLMCatalog(), fetchLLMConfig()])
      .then(([loadedCatalog, loadedConfig]) => {
        if (!active) return;
        setCatalog(loadedCatalog);
        setConfig(loadedConfig);
        setSelectedProvider(loadedConfig.provider);
        setSelectedModel(loadedConfig.model);
        setMaxTokens(loadedConfig.max_tokens);
        setEditingKey(!loadedConfig.has_key);
      })
      .catch((err: unknown) => {
        if (active) setLoadError(errorMessage(err));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const providers = useMemo(() => (catalog ? sortedProviders(catalog) : []), [catalog]);
  const currentProvider = providers.find((p) => p.key === selectedProvider) ?? null;
  const models = currentProvider?.models ?? [];
  const groqProvider = providers.find((p) => p.key === 'groq') ?? null;

  function resetFeedback() {
    setSavedJustNow(false);
    setSaveError(null);
    setTestResult(null);
    setTestError(null);
  }

  function handleSelectProvider(provider: LLMProvider) {
    if (provider.key === selectedProvider) return;
    setSelectedProvider(provider.key);
    const firstModel = provider.models[0] as LLMModel | undefined;
    setSelectedModel(firstModel?.id ?? '');
    setMaxTokens(firstModel?.default_max_tokens ?? 0);
    resetFeedback();
  }

  function handleSelectModel(model: LLMModel) {
    setSelectedModel(model.id);
    setMaxTokens(model.default_max_tokens);
    resetFeedback();
  }

  function handleReplaceKey() {
    setEditingKey(true);
    setClearKey(false);
    setApiKey('');
    resetFeedback();
  }

  function handleClearKey() {
    setClearKey(true);
    setEditingKey(false);
    setApiKey('');
    resetFeedback();
  }

  function handleCancelKeyEdit() {
    setEditingKey(false);
    setClearKey(false);
    setApiKey('');
    setShowKey(false);
    resetFeedback();
  }

  function buildInput(): LLMConfigInput {
    const base = { provider: selectedProvider, model: selectedModel, max_tokens: maxTokens };
    if (clearKey) return { ...base, api_key: null };
    if (editingKey && apiKey.trim() !== '') return { ...base, api_key: apiKey };
    return base;
  }

  const dirty = Boolean(
    config &&
      (selectedProvider !== config.provider ||
        selectedModel !== config.model ||
        maxTokens !== config.max_tokens ||
        clearKey ||
        (editingKey && apiKey.trim() !== '')),
  );

  // Warn on a hard navigate-away (reload/close/external link) with unsaved
  // changes. react-router v6 here has no data router, so useBlocker isn't
  // available for in-app navigation — this only covers the browser-level case.
  useEffect(() => {
    function handleBeforeUnload(event: BeforeUnloadEvent) {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = '';
    }
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [dirty]);

  async function handleTest() {
    setTesting(true);
    setTestError(null);
    setTestResult(null);
    try {
      setTestResult(await testLLMConfig(buildInput()));
    } catch (err) {
      setTestError(errorMessage(err));
    } finally {
      setTesting(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setSaveError(null);
    setSavedJustNow(false);
    try {
      const updated = await saveLLMConfig(buildInput());
      setConfig(updated);
      setSelectedProvider(updated.provider);
      setSelectedModel(updated.model);
      setMaxTokens(updated.max_tokens);
      setEditingKey(!updated.has_key);
      setClearKey(false);
      setApiKey('');
      setShowKey(false);
      setSavedJustNow(true);
    } catch (err) {
      setSaveError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      <PageHeader
        current="/settings/"
        title="Settings"
        subtitle="Choose the LLM provider and model used to plan outreach, and connect an API key"
      />

      <div className="container narrow settings-page">
        {loadError && <ErrorMessage>Failed to load settings: {loadError}</ErrorMessage>}

        {loading ? (
          <EmptyState>Loading settings…</EmptyState>
        ) : !catalog || !config ? null : (
          <>
            {groqProvider && (
              <div className="callout callout-groq">
                New here? <strong>{groqProvider.label}</strong> has a free tier with no
                credit card required — the fastest way to get outreach planning working.{' '}
                <a
                  href={groqProvider.api_key_url}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Get a {groqProvider.api_key_label} →
                </a>
              </div>
            )}

            <section className="settings-section">
              <h2>Provider</h2>
              <div className="provider-grid">
                {providers.map((provider) => (
                  <ProviderCard
                    key={provider.key}
                    provider={provider}
                    selected={provider.key === selectedProvider}
                    onSelect={() => handleSelectProvider(provider)}
                  />
                ))}
              </div>
            </section>

            <section className="settings-section">
              <h2>Model</h2>
              <div className="model-grid">
                {models.map((model) => (
                  <ModelCard
                    key={model.id}
                    model={model}
                    selected={model.id === selectedModel}
                    onSelect={() => handleSelectModel(model)}
                  />
                ))}
              </div>
            </section>

            <section className="settings-section">
              <h2>API key</h2>

              {currentProvider && (
                <p className="api-key-link">
                  <a
                    href={currentProvider.api_key_url}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    Get your {currentProvider.api_key_label} →
                  </a>
                </p>
              )}

              {config.key_source === 'environment' && (
                <p className="env-key-note">
                  Using <code>{envVarNameFor(selectedProvider)}</code> from the
                  environment.
                </p>
              )}

              {config.has_key && !editingKey && !clearKey ? (
                <div className="key-row">
                  <span className="key-masked">•••• {config.key_last_four}</span>
                  <button type="button" className="secondary action" onClick={handleReplaceKey}>
                    Replace
                  </button>
                  <button type="button" className="secondary action" onClick={handleClearKey}>
                    Clear
                  </button>
                </div>
              ) : clearKey ? (
                <div className="key-row">
                  <span className="key-clearing">Key will be removed on save.</span>
                  <button type="button" className="secondary action" onClick={handleCancelKeyEdit}>
                    Undo
                  </button>
                </div>
              ) : (
                <div className="key-row">
                  <input
                    type={showKey ? 'text' : 'password'}
                    value={apiKey}
                    onChange={(event) => {
                      setApiKey(event.target.value);
                      resetFeedback();
                    }}
                    placeholder={
                      currentProvider ? `${currentProvider.api_key_prefix}...` : 'API key'
                    }
                    autoComplete="off"
                  />
                  <button
                    type="button"
                    className="secondary action"
                    onClick={() => setShowKey((value) => !value)}
                  >
                    {showKey ? 'Hide' : 'Show'}
                  </button>
                  {config.has_key && (
                    <button
                      type="button"
                      className="secondary action"
                      onClick={handleCancelKeyEdit}
                    >
                      Cancel
                    </button>
                  )}
                </div>
              )}
            </section>

            <div className="settings-actions">
              <button type="button" onClick={handleTest} disabled={testing}>
                {testing ? 'Testing…' : 'Test connection'}
              </button>
              <button
                type="button"
                className="secondary"
                onClick={handleSave}
                disabled={!dirty || saving}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>

              {testError && <ErrorMessage>Test failed: {testError}</ErrorMessage>}
              {testResult &&
                (testResult.ok ? (
                  <span className="status test-ok">
                    Connected in {testResult.latency_ms}ms ({testResult.model_echo}).
                  </span>
                ) : (
                  <ErrorMessage>{humanTestError(testResult)}</ErrorMessage>
                ))}

              {saveError && <ErrorMessage>Save failed: {saveError}</ErrorMessage>}
              {savedJustNow && !dirty && <span className="status test-ok">Saved.</span>}
            </div>
          </>
        )}
      </div>
    </>
  );
}
