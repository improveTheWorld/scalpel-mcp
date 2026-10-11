window.__ModuleLoader__.load({
  id: '@local/dsh-tool-scalpel',
  factory(require) {
    const React = require('react');
    const h = React.createElement;

    let rootContext = null;

    // Meme regle que defaultModeForPreset (lib/toolset.js), qui ne peut pas etre importe cote navigateur.
    function defaultModeFor(preset) {
      return preset ? (String(preset).includes('scalpel') ? 'scalpel' : 'standard') : 'scalpel';
    }

    const LOCK = '\uD83D\uDD12';
    const ARROW = '\u25BE';
    const BOLT = '\u26A1';
    const MEMO = '\uD83D\uDCDD';
    // Delai maximal pendant lequel on affiche le mode demande en attendant la confirmation du journal.
    const PENDING_TIMEOUT_MS = 6000;

    function renderToolsetSelector(props) {
      const sessionId = props.sessionId;
      const containerRef = React.useRef(null);

      const [isOpen, setIsOpen] = React.useState(false);
      const [busy, setBusy] = React.useState(false);
      const [pending, setPending] = React.useState(null);

      // Etat reel de la session, observe en direct.
      const session = typeof props.useSession === 'function' ? props.useSession((s) => s) : null;
      const isBlank = session ? session.blank !== false : true;
      const isLocked = !isBlank;

      // Source de verite : projection 'toolset' (repliee depuis le journal de la session par l'hote).
      // `mode` est null tant que /toolset n'a jamais ete execute : on retombe alors sur le defaut du preset.
      const view = typeof props.useProjection === 'function' ? props.useProjection('toolset') : undefined;
      const preset = typeof props.useProjection === 'function' ? props.useProjection('agentPreset') : '';
      const loggedMode = (view && view.mode) || defaultModeFor(preset);

      // Le mode demande n'est affiche que le temps que le journal le confirme.
      React.useEffect(() => {
        if (pending === null) return;
        if (loggedMode === pending) {
          setPending(null);
          setBusy(false);
          return;
        }
        const timer = setTimeout(() => { setPending(null); setBusy(false); }, PENDING_TIMEOUT_MS);
        return () => clearTimeout(timer);
      }, [pending, loggedMode]);

      const mode = pending !== null ? pending : loggedMode;

      React.useEffect(() => {
        if (!isOpen) return;
        const onDocClick = (e) => {
          if (containerRef.current && !containerRef.current.contains(e.target)) {
            setIsOpen(false);
          }
        };
        document.addEventListener('pointerdown', onDocClick);
        return () => document.removeEventListener('pointerdown', onDocClick);
      }, [isOpen]);

      async function switchMode(newMode) {
        if (isLocked || busy || newMode === loggedMode) { setIsOpen(false); return; }
        setIsOpen(false);
        setBusy(true);
        setPending(newMode);

        const commandLine = `/toolset ${newMode}`;

        try {
          if (rootContext && rootContext.remote && rootContext.remote.commands && rootContext.remote.commands.execute && sessionId) {
            await rootContext.remote.commands.execute(sessionId, commandLine, []);
            return;
          }
        } catch (e) {
          console.warn('[toolset] remote command execute failed', e);
        }

        try {
          const actions = props.inputActions;
          if (actions && typeof actions.setDraft === 'function' && typeof actions.submit === 'function') {
            actions.setDraft(commandLine);
            actions.submit('queue');
            return;
          }
          if (actions && typeof actions.setDraft === 'function') {
            actions.setDraft(commandLine);
          }
        } catch (e) {
          console.warn('[toolset] inputActions submit failed', e);
        }
        setPending(null);
        setBusy(false);
      }

      const isScalpel = mode === 'scalpel';
      const labelText = busy ? 'Application...' : (isScalpel ? 'AST Scalpel' : 'Outils Plats');
      const modeName = isScalpel ? 'AST Scalpel' : 'Outils Plats';
      const tooltip = isLocked
        ? `Jeu d'outils fig\u00e9 pour cette session : ${modeName}. Changer avec /toolset scalpel|standard.`
        : "Choisir le jeu d'outils d'\u00e9dition de code pour cette session";

      return h('div', {
        ref: containerRef,
        style: {
          position: 'relative',
          display: 'inline-flex',
          alignItems: 'center',
          fontSize: '12px',
          marginRight: '6px',
          userSelect: 'none'
        }
      }, [
        h('button', {
          key: 'btn',
          type: 'button',
          onClick: () => { if (!isLocked && !busy) setIsOpen(!isOpen); },
          disabled: isLocked || busy,
          title: tooltip,
          style: {
            display: 'inline-flex',
            alignItems: 'center',
            gap: '5px',
            padding: '2px 8px',
            borderRadius: '12px',
            border: '1px solid ' + (isLocked ? 'var(--dsw-alias-border-l1, #3a3a3a)' : 'var(--dsw-alias-border-l2, #555)'),
            background: isLocked
              ? 'var(--dsw-alias-bg-layer-2, #252525)'
              : (isScalpel ? 'var(--dsw-alias-brand-primary, #1e88e5)' : 'var(--dsw-alias-bg-layer-2, #2a2a2a)'),
            color: isLocked
              ? 'var(--dsw-alias-label-secondary, #aaa)'
              : (isScalpel ? '#ffffff' : 'var(--dsw-alias-label-primary, #e0e0e0)'),
            cursor: isLocked ? 'default' : (busy ? 'wait' : 'pointer'),
            fontWeight: 500,
            opacity: isLocked ? 0.75 : (busy ? 0.6 : 1),
            transition: 'all 0.15s ease'
          }
        }, [
          h('span', {
            key: 'dot',
            style: { width: 6, height: 6, borderRadius: '50%', background: isScalpel ? '#4caf50' : '#ffa726' }
          }),
          h('span', { key: 'txt' }, labelText),
          isLocked
            ? h('span', { key: 'lock', style: { fontSize: '10px', opacity: 0.7 } }, LOCK)
            : h('span', { key: 'arrow', style: { fontSize: '9px', opacity: 0.8 } }, ARROW)
        ]),

        (!isLocked && isOpen) ? h('div', {
          key: 'menu',
          style: {
            position: 'absolute',
            bottom: 'calc(100% + 6px)',
            left: 0,
            zIndex: 1000,
            minWidth: '220px',
            background: 'var(--dsw-alias-bg-overlay, #222222)',
            border: '1px solid var(--dsw-alias-border-l2, #444)',
            borderRadius: '6px',
            padding: '4px',
            boxShadow: '0 4px 16px rgba(0,0,0,0.45)',
            display: 'flex',
            flexDirection: 'column',
            gap: '2px'
          }
        }, [
          optionRow('opt-scalpel', 'scalpel', isScalpel, BOLT + ' Scalpel (Graphe AST)', 'D\u00e9sactive les outils plats (read, edit, write)'),
          optionRow('opt-standard', 'standard', !isScalpel, MEMO + ' Standard (Fichiers plats)', 'R\u00e9active read, edit, write, grep, glob')
        ]) : null
      ]);

      function optionRow(key, target, selected, title, description) {
        return h('div', {
          key,
          onClick: () => switchMode(target),
          style: {
            padding: '6px 8px',
            borderRadius: '4px',
            cursor: 'pointer',
            background: selected ? 'var(--dsw-alias-bg-layer-2, #333)' : 'transparent',
            color: 'var(--dsw-alias-label-primary, #fff)',
            fontSize: '11px',
            display: 'flex',
            flexDirection: 'column'
          }
        }, [
          h('span', { key: 't', style: { fontWeight: 'bold' } }, title),
          h('span', { key: 'd', style: { fontSize: '10px', color: 'var(--dsw-alias-label-secondary, #aaa)' } }, description)
        ]);
      }
    }

    // Un composant qui leve est abdique en silence par le moteur de slots : on l'isole.
    function ToolsetSelector(props) {
      try {
        return renderToolsetSelector(props);
      } catch (err) {
        console.error('[toolset] ToolsetSelector render failure:', err);
        return null;
      }
    }

    return {
      inject: ['slots', 'remote'],
      apply(ctx) {
        rootContext = ctx;

        // Un SEUL affichage : la barre d'outils du composer, visible des l'instanciation (session vierge)
        // et pendant toute la session (verrouille ensuite). L'en-tete de session n'en porte plus.
        ctx.slots.inject('conversation.input.left', () => ctx.slots.register({
          name: 'conversation.input.left',
          id: 'toolset-composer-selector',
          order: -5,
          label: 'Toolset'
        }, ToolsetSelector));
      }
    };
  }
});