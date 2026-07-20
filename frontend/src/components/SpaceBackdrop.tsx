import { useRef, useMemo, useState, useEffect } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import * as THREE from 'three'

// ══════════════════════════════════════════════════════════════
// SPACE BACKDROP — subtle full-viewport ambience behind the HUD
// Slow-drifting starfield + faint horizon grid + mouse parallax.
// Deliberately dim: ambience, not distraction.
// ══════════════════════════════════════════════════════════════

const CYAN = '#00d4ff'

// ── Starfield — slow-drifting additive points ──────────────────
function Starfield() {
  const ref = useRef<THREE.Points>(null!)

  const N = 600
  const { positions, sizes } = useMemo(() => {
    const pos  = new Float32Array(N * 3)
    const size = new Float32Array(N)
    for (let i = 0; i < N; i++) {
      pos[i*3]   = (Math.random() - 0.5) * 40
      pos[i*3+1] = (Math.random() - 0.5) * 24
      pos[i*3+2] = -4 - Math.random() * 26
      size[i] = 0.02 + Math.random() * 0.05
    }
    return { positions: pos, sizes: size }
  }, [])

  useFrame((_, dt) => {
    if (ref.current) {
      ref.current.rotation.z += dt * 0.004
      ref.current.rotation.y += dt * 0.002
    }
  })

  return (
    <points ref={ref}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
        <bufferAttribute attach="attributes-size"     args={[sizes, 1]} />
      </bufferGeometry>
      <pointsMaterial
        color="#9adfff"
        size={0.045}
        sizeAttenuation
        transparent
        opacity={0.35}
        depthWrite={false}
        blending={THREE.AdditiveBlending}
        toneMapped={false}
      />
    </points>
  )
}

// ── Horizon grid — thin cyan lines with slight perspective drift ─
function HorizonGrid() {
  const ref = useRef<THREE.LineSegments>(null!)

  const geometry = useMemo(() => {
    const verts: number[] = []
    const W = 40, D = 30, STEP = 2
    // Lines running into the distance
    for (let x = -W / 2; x <= W / 2; x += STEP) {
      verts.push(x, 0, 0, x, 0, -D)
    }
    // Cross lines
    for (let z = 0; z >= -D; z -= STEP) {
      verts.push(-W / 2, 0, z, W / 2, 0, z)
    }
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.Float32BufferAttribute(verts, 3))
    return g
  }, [])

  useFrame((state) => {
    if (ref.current) {
      // Slight perspective scroll — grid slides toward the camera and wraps
      ref.current.position.z = (state.clock.elapsedTime * 0.15) % 2
    }
  })

  return (
    <lineSegments ref={ref} geometry={geometry} position={[0, -4.5, 0]}>
      <lineBasicMaterial color={CYAN} transparent opacity={0.04} depthWrite={false} toneMapped={false} />
    </lineSegments>
  )
}

// ── Camera parallax — lerped toward normalized mouse position ──
function ParallaxRig() {
  const { camera } = useThree()
  const mouse = useRef({ x: 0, y: 0 })

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      mouse.current.x = (e.clientX / window.innerWidth)  * 2 - 1
      mouse.current.y = (e.clientY / window.innerHeight) * 2 - 1
    }
    window.addEventListener('mousemove', onMove)
    return () => window.removeEventListener('mousemove', onMove)
  }, [])

  useFrame(() => {
    const targetY = -mouse.current.x * 0.15
    const targetX = -mouse.current.y * 0.15
    camera.rotation.y += (targetY - camera.rotation.y) * 0.04
    camera.rotation.x += (targetX - camera.rotation.x) * 0.04
  })

  return null
}

// ── Export ─────────────────────────────────────────────────────
export default function SpaceBackdrop() {
  // Pause rendering while the tab is hidden
  const [visible, setVisible] = useState(!document.hidden)
  useEffect(() => {
    const onVis = () => setVisible(!document.hidden)
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [])

  return (
    <div style={{ position: 'fixed', inset: 0, zIndex: 0, pointerEvents: 'none', opacity: 0.55 }}>
      <Canvas
        style={{ width: '100%', height: '100%', background: 'transparent' }}
        camera={{ position: [0, 0, 6], fov: 55 }}
        gl={{ alpha: true, antialias: false }}
        onCreated={({ gl }) => gl.setClearColor(0x000000, 0)}
        dpr={[1, 1.5]}
        frameloop={visible ? 'always' : 'never'}
      >
        <Starfield />
        <HorizonGrid />
        <ParallaxRig />
      </Canvas>
    </div>
  )
}
