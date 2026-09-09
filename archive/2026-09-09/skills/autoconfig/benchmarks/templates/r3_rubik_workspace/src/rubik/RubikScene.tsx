import type { CubeState } from "./cubeState";

export function RubikScene({ cube }: { cube: CubeState }) {
  return (
    <div className="scene-shell">
      <div data-testid="cube-viewport" aria-label="Rubik cube viewport">
        <p>3D cube placeholder</p>
        <p data-testid="cube-face-count">faces:{cube.faces.length}</p>
      </div>
    </div>
  );
}
