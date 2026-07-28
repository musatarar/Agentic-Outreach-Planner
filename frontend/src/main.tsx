import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { DashboardPage } from './pages/DashboardPage';
import { PlannerPage } from './pages/PlannerPage';
import { ReportsPage } from './pages/ReportsPage';
import { SettingsPage } from './pages/SettingsPage';
import './styles.css';

// The routes match the Django URLs in project/urls.py, so a hard refresh on any
// of them is served by Django (which also sets the csrftoken cookie) and React
// then takes over client-side navigation.
const root = document.getElementById('root');
if (!root) throw new Error('#root element not found');

createRoot(root).render(
  <StrictMode>
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<PlannerPage />} />
        <Route path="/reports/" element={<ReportsPage />} />
        <Route path="/next-actions/" element={<DashboardPage />} />
        <Route path="/settings/" element={<SettingsPage />} />
      </Routes>
    </BrowserRouter>
  </StrictMode>,
);
