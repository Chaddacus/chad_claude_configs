import { useMemo, useState } from "react";
import { createSolvedCube, scrambleCube } from "./rubik/cubeState";
import { solveCube } from "./rubik/solver";
import { RubikScene } from "./rubik/RubikScene";

const SCRAMBLE_SEED = 7;

export default function App() {
  const [cube, setCube] = useState(() => createSolvedCube());
  const [status, setStatus] = useState<"solved" | "scrambled" | "solving">("solved");

  const scrambleLabel = useMemo(() => `Seed ${SCRAMBLE_SEED}`, []);

  const handleScramble = () => {
    const scrambled = scrambleCube(cube, SCRAMBLE_SEED);
    setCube(scrambled);
    setStatus(scrambled.isSolved ? "solved" : "scrambled");
  };

  const handleSolve = () => {
    const solved = solveCube(cube);
    setCube(solved);
    setStatus(solved.isSolved ? "solved" : "solving");
  };

  const handleReset = () => {
    setCube(createSolvedCube());
    setStatus("solved");
  };

  return (
    <main className="app-shell">
      <section className="hero">
        <div>
          <p className="eyebrow">Rubik Benchmark</p>
          <h1>Bounded 3D cube starter</h1>
          <p className="lede">
            The viewport, solver, and verification harness exist. The benchmark
            is incomplete until scramble, solve, animation, and tests behave
            correctly.
          </p>
        </div>
        <div className="controls">
          <button onClick={handleScramble}>Scramble</button>
          <button onClick={handleSolve}>Solve</button>
          <button onClick={handleReset}>Reset</button>
        </div>
      </section>

      <section className="panel">
        <div className="status-row">
          <span data-testid="cube-status">{status}</span>
          <span data-testid="scramble-label">{scrambleLabel}</span>
        </div>
        <RubikScene cube={cube} />
      </section>
    </main>
  );
}
