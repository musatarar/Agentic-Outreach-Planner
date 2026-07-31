import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { RequireAuth } from './components/RequireAuth';
import { ConsumePage } from './pages/ConsumePage';
import { DashboardPage } from './pages/DashboardPage';
import { DonePage } from './pages/DonePage';
import { InboxPage } from './pages/InboxPage';
import { PlannerPage } from './pages/PlannerPage';
import { ReportsPage } from './pages/ReportsPage';
import { SettingsPage } from './pages/SettingsPage';
import { SignInPage } from './pages/SignInPage';
import './styles.css';

const root = document.getElementById('root');
if (!root) throw new Error('#root element not found');

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/signin" element={<SignInPage />} />
        <Route path="/auth/consume" element={<ConsumePage />} />
        <Route path="/" element={<RequireAuth><PlannerPage /></RequireAuth>} />
        <Route path="/inbox" element={<RequireAuth><InboxPage /></RequireAuth>} />
        <Route path="/done" element={<RequireAuth><DonePage /></RequireAuth>} />
        <Route path="/reports/" element={<RequireAuth><ReportsPage /></RequireAuth>} />
        <Route path="/next-actions/" element={<RequireAuth><DashboardPage /></RequireAuth>} />
        <Route path="/settings/" element={<RequireAuth><SettingsPage /></RequireAuth>} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
