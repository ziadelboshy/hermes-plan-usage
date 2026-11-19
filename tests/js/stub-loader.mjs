// Lets node import desktop/plugin.js offline: the Desktop-provided modules become small stubs.
const STUBS = {
  '@hermes/plugin-sdk': `const C = () => null;
    export const Button = C, Codicon = C, Input = C, ListRow = C, StatusDot = C, Switch = C, Tip = C, ToggleRow = C;
    export const PALETTE_AREA = 'palette', SETTINGS_PLUGINS_AREA = 'settings', STATUSBAR_AREAS = { right: 'right' };
    export const cn = (...a) => a.filter(Boolean).join(' '), haptic = () => {}, pluginSettingsHref = id => id;
    export const host = { notify() {}, request: async () => ({}) }, queryClient = { invalidateQueries: async () => {} };
    export const useQuery = () => ({});`,
  'react/jsx-runtime': 'export const Fragment = "f", jsx = () => null, jsxs = () => null;',
  react: 'export const useEffect = () => {}, useState = v => [v, () => {}];'
}

export async function resolve(specifier, context, next) {
  if (specifier in STUBS) return { url: `data:text/javascript,${encodeURIComponent(STUBS[specifier])}`, shortCircuit: true }
  return next(specifier, context)
}
