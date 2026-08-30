// jsdom implements no layout, so scrollIntoView does not exist there. Stub it rather
// than making the component defensive about an API every real browser has.
Element.prototype.scrollIntoView = () => {}
