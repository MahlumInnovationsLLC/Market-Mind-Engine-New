import CalibrationPanel from "@/components/intelligence/calibration-panel";

export default function CalibrationPage() {
  return (
    <div className="space-y-4 font-mono" data-testid="page-calibration">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold tracking-tight">Calibration</h1>
        <p className="text-xs uppercase tracking-widest text-muted-foreground">
          Reliability + sharpness across verticals · ECE / Brier / log-loss
        </p>
      </header>
      <CalibrationPanel />
    </div>
  );
}
