import Window from './components/Window'

const App = () => {
  const windowType = new URLSearchParams(window.location.search).get('window')
  const title = new URLSearchParams(window.location.search).get('title')

  if (windowType === 'child') {
    return <div>{title || 'Window'}</div>
  }

  return (
    <>
      <Window title="Window 1" />
      <Window title="Window 2" />
    </>
  )
}

export default App