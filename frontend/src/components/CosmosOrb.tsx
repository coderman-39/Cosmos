import { useRef, useMemo, useState, useEffect, Suspense } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { EffectComposer, Bloom } from '@react-three/postprocessing'
import * as THREE from 'three'
import { CosmosState, useCosmosStore } from '../store'

// ── Color per state ────────────────────────────────────────────
const COLORS: Record<CosmosState, string> = {
  sleeping:  '#0a2a50',
  waking:    '#0044cc',
  idle:      '#0077ff',
  listening: '#00d4ff',
  thinking:  '#ff8800',
  speaking:  '#00ff88',
  executing: '#9944ff',
}

const GLOW: Record<CosmosState, string> = {
  sleeping:  'rgba(10,42,80,0.12)',
  waking:    'rgba(0,68,204,0.25)',
  idle:      'rgba(0,119,255,0.28)',
  listening: 'rgba(0,212,255,0.45)',
  thinking:  'rgba(255,136,0,0.42)',
  speaking:  'rgba(0,255,136,0.42)',
  executing: 'rgba(153,68,255,0.40)',
}

const SPEED: Record<CosmosState, number> = {
  sleeping: 0.08, waking: 0.22, idle: 0.14,
  listening: 0.22, thinking: 0.26, speaking: 0.20, executing: 0.18,
}

// Orbit speedup while the agent executes
const orbitSpeed = (state: CosmosState) =>
  SPEED[state] * (state === 'executing' ? 1.6 : 1)

// ── Nebula cloud — random sphere distribution ──────────────────
function NebulaCloud({ state }: { state: CosmosState }) {
  const ref = useRef<THREE.Points>(null!)
  const col = COLORS[state]

  const N    = 1600
  const { positions, sizes } = useMemo(() => {
    const pos  = new Float32Array(N * 3)
    const size = new Float32Array(N)
    for (let i = 0; i < N; i++) {
      // Cluster more particles toward center for natural density gradient
      const r  = 0.4 + Math.pow(Math.random(), 0.6) * 1.4
      const th = Math.random() * Math.PI * 2
      const ph = Math.acos(2 * Math.random() - 1)
      pos[i*3]   = r * Math.sin(ph) * Math.cos(th)
      pos[i*3+1] = r * Math.sin(ph) * Math.sin(th)
      pos[i*3+2] = r * Math.cos(ph)
      size[i] = 0.012 + Math.random() * 0.030
    }
    return { positions: pos, sizes: size }
  }, [])

  useFrame((_, dt) => {
    if (ref.current) {
      ref.current.rotation.y += dt * orbitSpeed(state) * 0.5
      ref.current.rotation.x += dt * orbitSpeed(state) * 0.18
    }
  })

  return (
    <points ref={ref}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
        <bufferAttribute attach="attributes-size"     args={[sizes, 1]} />
      </bufferGeometry>
      <pointsMaterial
        color={col}
        size={0.022}
        sizeAttenuation
        transparent
        opacity={state === 'sleeping' ? 0.20 : 0.70}
        depthWrite={false}
        blending={THREE.AdditiveBlending}
        toneMapped={false}
      />
    </points>
  )
}

// ── Orbital band — concentrated ring of particles ─────────────
function OrbitalBand({
  state, radius, tiltX, tiltZ, speedScale, opacity = 0.85,
}: {
  state: CosmosState; radius: number; tiltX: number; tiltZ: number
  speedScale: number; opacity?: number
}) {
  const ref = useRef<THREE.Points>(null!)
  const col = COLORS[state]

  const N   = 280
  const pos = useMemo(() => {
    const p = new Float32Array(N * 3)
    for (let i = 0; i < N; i++) {
      const a    = (i / N) * Math.PI * 2 + Math.random() * 0.25
      const jitter = (Math.random() - 0.5) * 0.12
      p[i*3]   = (radius + jitter) * Math.cos(a)
      p[i*3+1] = (Math.random() - 0.5) * 0.18
      p[i*3+2] = (radius + jitter) * Math.sin(a)
    }
    return p
  }, [radius])

  useFrame((_, dt) => {
    if (ref.current) {
      ref.current.rotation.y += dt * orbitSpeed(state) * speedScale
    }
  })

  return (
    <points ref={ref} rotation={[tiltX, 0, tiltZ]}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[pos, 3]} />
      </bufferGeometry>
      <pointsMaterial
        color={col}
        size={0.028}
        sizeAttenuation
        transparent
        opacity={state === 'sleeping' ? 0.08 : opacity}
        depthWrite={false}
        blending={THREE.AdditiveBlending}
        toneMapped={false}
      />
    </points>
  )
}

// ── Central core glow — bright center ─────────────────────────
function CoreGlow({ state }: { state: CosmosState }) {
  const ref  = useRef<THREE.Mesh>(null!)
  const ref2 = useRef<THREE.Mesh>(null!)
  const col  = useMemo(() => new THREE.Color(COLORS[state]), [state])

  useFrame(() => {
    // Read the audio level imperatively — subscribing via a hook would
    // re-render the React tree at 60fps for a value only used per-frame here
    const audioLevel = useCosmosStore.getState().audioLevel
    const t     = Date.now() * 0.001
    // Use slow sine for all states — listening just has a slightly wider wave
    const pulse = 1 + Math.sin(t * 1.2) * 0.04
    // Audio level smoothed — small bump, no sudden jumps
    const bump  = 1 + audioLevel * 0.10
    if (ref.current)  ref.current.scale.setScalar(pulse * bump * 0.32)
    if (ref2.current) ref2.current.scale.setScalar(pulse * bump * 0.60)
  })

  return (
    <group>
      {/* Tight white-hot center */}
      <mesh ref={ref}>
        <sphereGeometry args={[1, 16, 16]} />
        <meshBasicMaterial color="#ffffff" toneMapped={false} />
      </mesh>
      {/* Coloured halo */}
      <mesh ref={ref2}>
        <sphereGeometry args={[1, 16, 16]} />
        <meshBasicMaterial
          color={col} transparent opacity={state === 'sleeping' ? 0.06 : 0.45}
          blending={THREE.AdditiveBlending} depthWrite={false} toneMapped={false}
        />
      </mesh>
    </group>
  )
}

// ── Full scene ─────────────────────────────────────────────────
function Scene({ state }: { state: CosmosState }) {
  return (
    <>
      <CoreGlow state={state} />
      <NebulaCloud state={state} />

      {/* Three orbital bands at different tilts */}
      <OrbitalBand state={state} radius={1.10} tiltX={Math.PI * 0.10} tiltZ={0}             speedScale={1.0}  opacity={0.90} />
      <OrbitalBand state={state} radius={1.35} tiltX={Math.PI * 0.35} tiltZ={Math.PI * 0.2} speedScale={-0.7} opacity={0.75} />
      <OrbitalBand state={state} radius={1.55} tiltX={Math.PI * 0.55} tiltZ={Math.PI * 0.4} speedScale={0.5}  opacity={0.55} />

      {/* Bloom lifts the additive particles into a proper energy glow */}
      <EffectComposer multisampling={0}>
        <Bloom intensity={1.2} luminanceThreshold={0.18} mipmapBlur />
      </EffectComposer>
    </>
  )
}

// ── Export ─────────────────────────────────────────────────────
interface Props { state: CosmosState }

export default function CosmosOrb({ state }: Props) {
  // Battery hygiene: no 60fps render loop while the tab is hidden ('never')
  // or Cosmos sleeps ('demand' — one frame per state change).
  const [frameloop, setFrameloop] = useState<'always' | 'demand' | 'never'>('always')
  useEffect(() => {
    const compute = () =>
      setFrameloop(document.hidden ? 'never' : state === 'sleeping' ? 'demand' : 'always')
    compute()
    document.addEventListener('visibilitychange', compute)
    return () => document.removeEventListener('visibilitychange', compute)
  }, [state])

  return (
    <div style={{ position: 'relative', width: '100%', height: '100%' }}>

      {/* CSS atmosphere — compact glow hugging the core (no big outer disc) */}
      <div style={{
        position: 'absolute', inset: 0,
        borderRadius: '50%',
        background: `radial-gradient(circle at 50% 50%, ${GLOW[state]} 0%, transparent 45%)`,
        filter: 'blur(16px)',
        transform: 'scale(0.95)',
        pointerEvents: 'none',
        transition: 'background 0.5s ease',
        zIndex: 0,
      }} />

      {/* Canvas — explicitly oversized (200%) and CENTERED on the orb box via
          transform (not `inset`, which R3F's wrapper collapses). The camera is
          pulled back so the orb keeps its size, and a radial mask fades the
          square edges to full transparency — the bloom haze dissolves into the
          background instead of ending at a visible box. */}
      <Canvas
        style={{
          position: 'absolute', top: '50%', left: '50%',
          width: '200%', height: '200%', transform: 'translate(-50%, -50%)',
          zIndex: 1, background: 'transparent', pointerEvents: 'none',
          WebkitMaskImage:
            'radial-gradient(circle at 50% 50%, #000 44%, rgba(0,0,0,0.4) 56%, transparent 67%)',
          maskImage:
            'radial-gradient(circle at 50% 50%, #000 44%, rgba(0,0,0,0.4) 56%, transparent 67%)',
        }}
        camera={{ position: [0, 0, 10.0], fov: 44 }}
        gl={{ alpha: true, antialias: true, toneMapping: THREE.NoToneMapping }}
        onCreated={({ gl }) => gl.setClearColor(0x000000, 0)}
        dpr={[1, 1.5]}
        frameloop={frameloop}
      >
        <Suspense fallback={null}>
          <Scene state={state} />
        </Suspense>
      </Canvas>
    </div>
  )
}
